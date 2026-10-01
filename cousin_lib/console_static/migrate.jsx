// The kind switch and lifecycle: this package's own file (index.html's package block).
// Its routes are cousin_lib/console/routes_migrate.py and routes_lifecycle.py.
//
// The kind switch dialog: cousin-migrate for a tmux-lane cousin (plan with
// --account and --validate, then apply) and the kind switch for a
// runner cousin (--to sdk|tmux), in one dialog. It opens on the window event
// `fw-open-kind-switch` {slug}, which the agent panel (agent.jsx) and this file's
// own inspector button send, so it is mounted once, outside the inspector.
// apply and rollback are the cousin's long operation (console/longop.py):
// their steps show as they happen, and when the pane waits on a person
// (the trust dialog: `screen: trust`), a button opens the cousin's pane,
// chat.jsx's PaneView on the tmux kind's own socket, to answer it in.
//
// The inspector gets two panels (slot inspector.panels): "kind and
// migration" (the records, check, rollback) and "lifecycle" (reincarnate,
// transplant). Destructive steps ask twice; a soul donation or a body swap
// is typed, and a forced rollback asks a third time. The screens, the kinds
// with a pane and the op kinds come from the server (GET .../migrate,
// GET /api/lifecycle/modes), never a copy here.

const MIG_SMALL = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const MIG_MONO = { fontFamily: "var(--mono)", fontSize: 11 };
const MIG_HINT = { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" };
const MIG_ROW = { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" };
const MIG_COL = { display: "flex", flexDirection: "column", gap: 8 };

// ---- pure helpers
const inList = (list, item) => !!item && (list || []).indexOf(item) >= 0;

// The op's stages in the order it planned them (params.steps), the ones
// not reached yet as "pending" (or "not run" once the op ended), then any
// stage it reported outside the plan.
function migrateStageRows(op) {
  if (!op) return [];
  const stages = op.stages || [];
  const planned = (op.params && op.params.steps) || [];
  const byName = {};
  stages.forEach(s => { byName[s.name] = s; });
  const rows = [];
  const seen = {};
  planned.forEach(name => {
    seen[name] = true;
    rows.push(byName[name] || { name, status: op.status === "running" ? "pending" : "not run", detail: null });
  });
  stages.forEach(s => { if (!seen[s.name]) rows.push(s); });
  return rows;
}

// The screen a detail line names ("... (screen: trust)"), when it is one
// of `screens` (the server's person_screens), else null.
function screenInDetail(detail, screens) {
  const m = /screen: ([a-z_]+)/.exec(String(detail || ""));
  return m && inList(screens, m[1]) ? m[1] : null;
}

// What the pane waits on: a running stage's detail first, then the
// cousin's login-required state ({screen}), else null.
function paneWaitScreen(op, loginScreen, screens) {
  const stages = (op && op.stages) || [];
  for (let i = stages.length - 1; i >= 0; i--) {
    if (stages[i].status !== "running") continue;
    const s = screenInDetail(stages[i].detail, screens);
    if (s) return s;
  }
  const s = loginScreen && loginScreen.screen;
  return inList(screens, s) ? s : null;
}

// "pane" when the console's pane answers that screen (the server's
// pane_answers), "terminal" for a flow that takes several screens, else null.
function paneAction(screen, answers) {
  if (!screen) return null;
  return inList(answers, screen) ? "pane" : "terminal";
}

function paneButtonLabel(screen) {
  return screen === "trust" ? "open the pane to accept the trust dialog"
                            : `open the pane to answer it (screen: ${screen})`;
}

// The rollbacks the records allow: the kind switch back to the kind it
// came from. (2.0.0 has no legacy lane, so no migration rolls back to it.)
function rollbackOffers(state) {
  const out = [];
  const sw = state && state.switch;
  if (sw && ["switching", "switched", "failed"].indexOf(sw.state) >= 0 && sw.from) {
    out.push({ which: "switch", to: sw.from, label: `roll the kind switch back to ${sw.from}` });
  }
  return out;
}

// What the operator types to confirm a mode, from the server's phrase
// template (routes_lifecycle.confirm_phrase), "" when it has none.
function confirmPhrase(template, donor, recipient) {
  if (!template) return "";
  return String(template).replace("{donor}", donor).replace("{recipient}", recipient);
}

// The plan/apply body for the dialog's options: the tmux-lane migration
// takes an account and validate, the kind switch only `to`.
function migrateBody(legacy, opts) {
  if (!legacy) return { to: opts.to };
  const body = { validate: !!opts.validate };
  if (opts.account) body.account = opts.account;
  return body;
}

// The other kind a runner cousin can switch to, "" when there is none.
function otherKind(lane, kinds) {
  const list = (kinds || []).filter(k => k !== lane);
  return list.length ? list[0] : "";
}
// ---- end pure helpers

// A POST that always answers: a network error is { ok: false, status: 0 }
// with its words, so no caller is left busy.
async function migPost(path, body) {
  try {
    const { r, d } = await apiSend("POST", path, body || {});
    return { ok: r.ok, status: r.status, d: d || {} };
  } catch (e) {
    return { ok: false, status: 0, d: { error: "the console did not answer: " + String(e.message || e) } };
  }
}

// A GET with its error: { d } or { error } (the server's words, else the
// network's).
async function migGet(path) {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return { error: (d && d.error) || `HTTP ${r.status}` };
    return { d };
  } catch (e) {
    return { error: "the console did not answer: " + String(e.message || e) };
  }
}

function MigLoadError({ error }) {
  if (!error) return null;
  return <div data-migrate-load-error style={{ ...MIG_MONO, color: "var(--red)" }}>could not load: {error}</div>;
}

// GET /api/cousins/<slug>/migrate, reloaded when the cousin's op moves and
// every 10 s (a pane screen answered outside an op clears on its own).
function useMigrateState(slug) {
  const [state, setState] = React.useState(null);
  const [error, setError] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!slug) { setState(null); return; }
    const res = await migGet(`/api/cousins/${slug}/migrate`);
    if (res.d) { setState(res.d); setError(null); } else setError(res.error);
  }, [slug]);
  React.useEffect(() => {
    load();
    const on = (e) => { if ((e.detail || {}).slug === slug) load(); };
    window.addEventListener("fw-cousin-op", on);
    const tick = slug ? setInterval(load, 10000) : null;
    return () => { window.removeEventListener("fw-cousin-op", on); if (tick) clearInterval(tick); };
  }, [slug, load]);
  return [state, load, error];
}

function MigMsg({ msg }) {
  if (!msg) return null;
  return <div style={{ ...MIG_MONO, color: msg.err ? "var(--red)" : "var(--fg-2)", overflowWrap: "anywhere" }}>{msg.text}</div>;
}

// The pane of a tmux-kind cousin in a modal: chat.jsx's PaneView over the
// pane routes, which address the kind's own socket. Keys go in only while
// the pane waits on a person; the runner types everywhere else.
function MigratePaneModal({ slug, onClose }) {
  const PV = window.PaneView;
  // it opens over the switch dialog: a click on its backdrop closes it alone
  const close = (e) => { if (e) e.stopPropagation(); onClose(); };
  return (
    <div className="modal-bg" onClick={close}>
      <div className="modal" data-migrate-pane style={{ width: "min(1100px, 96vw)", maxWidth: "96vw" }}
           onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>{slug}: the pane</span>
          <span style={{ ...MIG_HINT, marginLeft: 8 }}>
            answer the dialog here (arrows and Enter); keys go in only while the pane waits on a person
          </span>
          <button className="close" onClick={close} style={{ marginLeft: "auto" }}>x</button>
        </div>
        <div style={{ display: "flex", flexDirection: "column", height: "70vh", minHeight: 0 }}>
          {PV ? <PV cousin={{ slug, tmuxSession: "tmux-" + slug }} onClose={onClose} serial />
              : <div style={{ ...MIG_MONO, padding: 20 }}>the terminal view is not loaded</div>}
        </div>
      </div>
    </div>
  );
}

// Whether the cousin has a pane the console can show, now or once `op`
// switched it (the server's pane_kinds).
function hasPane(state, op) {
  const kinds = state && state.pane_kinds;
  return inList(kinds, state && state.lane) || inList(kinds, op && op.params && op.params.to);
}

// What the pane waits on, for a panel: the screen and whether the console's
// pane answers it; null when there is no pane or nothing waits.
function paneWait(state, op) {
  if (!state || !hasPane(state, op)) return null;
  // a running op's own words first; after it, what the pane still shows
  const screen = op && op.status === "running"
    ? paneWaitScreen(op, state.loginScreen, state.person_screens)
    : paneWaitScreen(null, state.loginScreen, state.person_screens);
  return screen ? { screen, action: paneAction(screen, state.pane_answers) } : null;
}

// The pane button, or for a flow of several screens (a login, onboarding)
// where to finish it.
function PaneWait({ wait, slug, onOpenPane }) {
  if (!wait) return null;
  if (wait.action === "pane") {
    return (
      <div style={MIG_ROW}>
        <button className="btn primary" style={MIG_SMALL} data-open-pane onClick={onOpenPane}>{paneButtonLabel(wait.screen)}</button>
        <span style={MIG_HINT}>nothing else types into that screen; one key at a time</span>
      </div>
    );
  }
  return (
    <div style={{ ...MIG_MONO, color: "var(--amber)" }} data-pane-terminal>
      the pane waits on the {wait.screen} screen, a flow of several screens: finish it in a terminal
      attached to the pane (the framework's tmux socket, run/tmux.sock, session tmux-{slug})
    </div>
  );
}

// A migrate or lifecycle op's stages, the planned ones not reached yet
// greyed, and what the pane waits on (`state` is GET .../migrate; a
// lifecycle op passes none).
function MigrateOpStages({ op, state, onOpenPane }) {
  if (!op) return null;
  const rows = migrateStageRows(op);
  const wait = paneWait(state, op);
  const p = op.params || {};
  const what = [p.to && `to ${p.to}`, p.account && `account ${p.account}`, p.validate && "validated",
                p.mode, p.donor && `donor ${p.donor}`, p.new_role && `role "${p.new_role}"`,
                p.force && "forced"].filter(Boolean).join(" · ");
  return (
    <div className="longop" data-migrate-op={op.kind}>
      <div className="longop-hdr">
        <StatePill state={op.status} />
        <span className="mono">{op.kind}</span>
        {what && <span className="muted">{what}</span>}
      </div>
      <ol className="longop-stages">
        {rows.map(s => (
          <li key={s.name}>
            <Led state={s.status === "skipped" ? "disabled" : s.status === "pending" || s.status === "not run" ? "stopped" : s.status}
                 pulse={s.status === "running"} />
            <span>{s.name}</span>
            {s.status === "pending" || s.status === "not run" ? <span className="muted"> · {s.status}</span> : null}
            {s.detail && <span className="muted"> · {s.detail}</span>}
          </li>
        ))}
      </ol>
      <PaneWait wait={wait} slug={op.slug} onOpenPane={onOpenPane} />
      {op.error && <div className="longop-error">{op.error}</div>}
    </div>
  );
}

function PlanChecklist({ plan }) {
  if (!plan) return null;
  const head = plan.from !== undefined
    ? `${plan.slug}: ${plan.from || "?"} -> ${plan.to}`
    : `${plan.slug}: the tmux lane -> the sdk runner, account ${plan.account}`;
  return (
    <div data-migrate-plan style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={MIG_ROW}>
        <span style={MIG_MONO}>{head}</span>
        <Pill tone={plan.ready ? "green" : "red"}>{plan.ready ? "ready" : "not ready"}</Pill>
      </div>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }}>
        {(plan.checks || []).map((c, i) => (
          <li key={c.check + i} style={{ ...MIG_MONO, display: "flex", gap: 6, alignItems: "baseline" }}>
            <Led state={c.ok ? "ok" : "failed"} />
            <span style={{ minWidth: 70, color: "var(--fg-1)" }}>{c.check}</span>
            <span style={{ color: c.ok ? "var(--fg-2)" : "var(--red)", overflowWrap: "anywhere" }}>
              {c.check === "carry" && c.ok && plan.carry
                ? (plan.carry.rows || []).map((r, j) => <div key={j}>{r.action}: {r.detail}</div>)
                : c.detail}
            </span>
          </li>
        ))}
      </ul>
      {(plan.warnings || []).map((w, i) => <div key={"w" + i} style={{ ...MIG_MONO, color: "var(--amber)" }}>warn: {w}</div>)}
      {(plan.notes || []).map((n, i) => <div key={"n" + i} style={MIG_HINT}>note: {n}</div>)}
      <div style={MIG_HINT}>steps: {(plan.steps || []).join(" -> ")}</div>
    </div>
  );
}

function DeferredList({ items }) {
  if (!items || !items.length) return null;
  return (
    <div data-migrate-deferred style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {items.map(d => (
        <div key={d.id} style={MIG_HINT}>
          <button className="btn ghost" style={MIG_SMALL} disabled title={d.why}>{d.label}</button>{" "}
          not yet: {d.why.replace(/^not yet: /, "")}
        </div>
      ))}
    </div>
  );
}

// The dialog: plan (a checklist), then apply as the cousin's op.
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
    migGet("/api/accounts").then(res => {
      if (res.d) setAccountNames((res.d.accounts || []).map(a => a.name));
      else setAccountsError(res.error);
    });
  }, [legacy]);

  const opts = { to, account, validate };
  const key = JSON.stringify(migrateBody(legacy, opts));
  const planFits = plan && planFor === key;
  React.useEffect(() => { setArmed(false); }, [key]);

  // a validating plan is an op: its plan is the op's result, for the
  // options it was asked with
  const planKeyRef = React.useRef(null);
  React.useEffect(() => {
    if (!planOpId || !op || op.id !== planOpId || op.status === "running") return;
    if (op.result && op.result.plan) { setPlan(op.result.plan); setPlanFor(planKeyRef.current); }
    else setMsg({ err: true, text: op.error || "the plan failed" });
    setPlanOpId(null);
  }, [planOpId, op && op.id, op && op.status]);

  const runPlan = async () => {
    setBusy(true); setMsg(null); setPlan(null);
    planKeyRef.current = key;
    const res = await migPost(`/api/cousins/${slug}/migrate/plan`, migrateBody(legacy, opts));
    setBusy(false);
    if (res.status === 202) { setPlanOpId(res.d.op.id); return; }
    if (!res.ok) { setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` }); return; }
    setPlan(res.d.plan); setPlanFor(key);
  };
  const apply = async () => {
    if (!armed) { setArmed(true); return; }
    setArmed(false); setBusy(true); setMsg(null);
    const res = await migPost(`/api/cousins/${slug}/migrate/apply`, { ...migrateBody(legacy, opts), confirm: true });
    setBusy(false);
    if (!res.ok) { setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` }); return; }
    setApplyOpId(res.d.op.id);
    reloadState();
  };

  const opRunning = op && op.status === "running";
  const shownOp = op && (op.id === applyOpId || op.id === planOpId
                         || (opRunning && inList(state && state.op_kinds, op.kind))) ? op : null;

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" data-kind-switch={slug} style={{ width: "min(720px, 96vw)" }} onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>{legacy ? `migrate ${slug} to the runner` : `switch ${slug}'s kind`}</span>
          <span style={{ ...MIG_HINT, marginLeft: 8 }}>cousin-migrate{legacy ? "" : " --to"}</span>
          <button className="close" onClick={onClose} style={{ marginLeft: "auto" }}>x</button>
        </div>
        <div className="body" style={MIG_COL}>
          <MigLoadError error={loadError} />
          {!state && !loadError && <div style={MIG_HINT}>loading...</div>}
          {state && (
            <>
              <div style={MIG_MONO}>
                lane now: <b>{lane}</b>
                {state.supervisor ? "" : " · no cousin-supervisor runs: apply needs it"}
                {state.running && state.running.slug !== slug ? ` · a ${state.running.kind} runs on ${state.running.slug}: one at a time` : ""}
              </div>
              {legacy ? (
                <div style={MIG_COL}>
                  <div style={MIG_ROW}>
                    <label style={MIG_MONO}>account</label>
                    <select className="sel" value={account} onChange={e => setAccount(e.target.value)}>
                      <option value="">(what the cousin names, else the host login)</option>
                      {accountNames.map(n => <option key={n} value={n}>{n}</option>)}
                    </select>
                    {accountsError && <span style={{ ...MIG_HINT, color: "var(--red)" }}>accounts not loaded: {accountsError}</span>}
                  </div>
                  <label style={{ ...MIG_MONO, display: "flex", gap: 6, alignItems: "flex-start" }}>
                    <input type="checkbox" checked={validate} onChange={e => setValidate(e.target.checked)} />
                    <span>validate: spends one model turn (the smallest one, on the model, effort and account
                      the runner will run, on a throwaway client). The plan spends one and the apply spends
                      its own. A carried model is never written without it.</span>
                  </label>
                </div>
              ) : (
                <div style={MIG_ROW}>
                  <label style={MIG_MONO}>to</label>
                  <select className="sel" value={to} onChange={e => setTo(e.target.value)}>
                    {(state.kinds || []).map(k => <option key={k} value={k}>{k}{k === lane ? " (now)" : ""}</option>)}
                  </select>
                  <span style={MIG_HINT}>the session goes on: the source stops at idle, the target resumes it</span>
                </div>
              )}
              <div style={MIG_ROW}>
                <button className="btn" disabled={busy || !!planOpId || (!legacy && !to)} onClick={runPlan}>
                  {planOpId ? "planning..." : validate && legacy ? "plan (one model turn)" : "plan"}
                </button>
                <span style={MIG_HINT}>writes nothing</span>
              </div>
              {planFits && <PlanChecklist plan={plan} />}
              {plan && !planFits && <div style={MIG_HINT}>the options changed: plan again</div>}
              <DeferredList items={state.deferred} />
              {shownOp && <MigrateOpStages op={shownOp} state={state} onOpenPane={() => setPaneOpen(true)} />}
              {!shownOp && <PaneWait wait={paneWait(state, null)} slug={slug} onOpenPane={() => setPaneOpen(true)} />}
              <MigMsg msg={msg} />
            </>
          )}
        </div>
        <div className="foot">
          <button className="btn" onClick={onClose}>close</button>
          <button className="btn primary" disabled={busy || opRunning || !planFits || !plan.ready || !state || !state.supervisor}
                  onClick={apply}
                  title={planFits && plan.ready ? "" : "a ready plan for these options comes first"}>
            {armed ? `click again: this stops and restarts ${slug}` : legacy ? "apply the migration" : `switch to ${to}`}
          </button>
        </div>
      </div>
      {paneOpen && <MigratePaneModal slug={slug} onClose={() => setPaneOpen(false)} />}
    </div>
  );
}

// Mounted once, outside the inspector: opens the dialog on the event.
function KindSwitchHost() {
  const [slug, setSlug] = React.useState(null);
  React.useEffect(() => {
    const on = (e) => { const s = (e.detail || {}).slug; if (s) setSlug(s); };
    window.addEventListener("fw-open-kind-switch", on);
    return () => window.removeEventListener("fw-open-kind-switch", on);
  }, []);
  if (!slug) return null;
  return <KindSwitchDialog key={slug} slug={slug} onClose={() => setSlug(null)} />;
}

function openKindSwitchDialog(slug) {
  window.dispatchEvent(new CustomEvent("fw-open-kind-switch", { detail: { slug } }));
}

// ---- the inspector: kind and migration --------------------------------------

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
    setBusy(true); setMsg(null); setReport(null);
    const body = { validate };
    if (since.trim()) body.since = since.trim();
    const res = await migPost(`/api/cousins/${slug}/migrate/check`, body);
    setBusy(false);
    if (res.status === 202) { setOpId(res.d.op.id); return; }
    if (!res.ok) { setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` }); return; }
    setReport(res.d.check);
  };
  const c = report;
  return (
    <div data-migrate-check style={MIG_COL}>
      <div style={MIG_ROW}>
        <input className="txt" style={{ width: 190 }} value={since} placeholder="since (ISO time, optional)"
               onChange={e => setSince(e.target.value)} />
        <label style={{ ...MIG_HINT, display: "flex", gap: 4, alignItems: "center" }}>
          <input type="checkbox" checked={validate} onChange={e => setValidate(e.target.checked)} />
          validate (spends one model turn)
        </label>
        <button className="btn" style={MIG_SMALL} disabled={busy || !!opId} onClick={run}>{opId ? "checking..." : "check"}</button>
      </div>
      {c && (
        <div className="longop" data-migrate-report>
          <div className="longop-hdr"><Pill tone={c.ok ? "green" : "red"}>{c.ok ? "ok" : "not ok"}</Pill>
            <span className="muted">since {c.since ? new Date(c.since * 1000).toISOString() : "the start"}</span></div>
          <div style={MIG_MONO}>
            inbox: {c.inbox_readable ? "" : "UNREADABLE · "}{c.inbox.done} done ({c.inbox.failed} failed), {c.inbox.open} open, {c.inbox.stale} stale
          </div>
          <div style={MIG_MONO}>tool calls: {c.tool_calls}, unrecorded: {(c.unrecorded || []).join(", ") || "none"}</div>
          <div style={MIG_MONO}>recorder hook errors: {(c.hook_errors || []).join("; ") || "none"}</div>
          {c.chat !== undefined && <div style={MIG_MONO}>chat server: {c.chat}</div>}
          <div style={MIG_MONO}>runner CLI: {c.cli}</div>
          {c.validate !== undefined && <div style={{ ...MIG_MONO, color: c.validate_ok ? "var(--fg-2)" : "var(--red)" }}>{c.validate_ok ? "validate:" : "NOT VALID:"} {c.validate}</div>}
          {c.config && c.config.runner && (
            <div style={MIG_MONO}>runner config: {Object.entries(c.config.runner).map(([k, v]) => `${k}=${v}`).join(", ")}</div>
          )}
          {(c.mismatches || []).map((m, i) => <div key={"m" + i} style={{ ...MIG_MONO, color: "var(--red)" }}>MISMATCH {m}</div>)}
          {(c.warnings || []).map((w, i) => <div key={"w" + i} style={{ ...MIG_MONO, color: "var(--amber)" }}>warn {w}</div>)}
        </div>
      )}
      <MigMsg msg={msg} />
    </div>
  );
}

// One rollback: a confirm; force asks a second time.
function RollbackButton({ slug, offer, disabled, onStarted }) {
  const [armed, setArmed] = React.useState(0);
  const [force, setForce] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const needed = offer.which === "migration" && force ? 2 : 1;
  const click = async () => {
    if (armed < needed) { setArmed(armed + 1); return; }
    setArmed(0); setBusy(true); setMsg(null);
    const body = { which: offer.which, confirm: true };
    if (offer.to) body.to = offer.to;
    if (offer.which === "migration" && force) { body.force = true; body.force_confirm = true; }
    const res = await migPost(`/api/cousins/${slug}/migrate/rollback`, body);
    setBusy(false);
    if (!res.ok) setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
    else if (onStarted) onStarted();
  };
  const label = armed === 0 ? offer.label
    : armed === 1 && needed === 1 ? "click again to roll back: the runner stops and the saved cousin.toml comes back"
    : armed === 1 ? "click again to roll back, forced"
    : "force: inbox rows still waiting are left unread on the tmux lane. Click again to force";
  return (
    <div data-migrate-rollback={offer.which} style={MIG_COL}>
      <div style={MIG_ROW}>
        <button className={`btn ${armed ? "danger" : ""}`} style={MIG_SMALL} disabled={disabled || busy} onClick={click}>{label}</button>
        {armed > 0 && <button className="btn ghost" style={MIG_SMALL} onClick={() => setArmed(0)}>keep</button>}
        {offer.which === "migration" && (
          <label style={{ ...MIG_HINT, display: "flex", gap: 4, alignItems: "center" }}>
            <input type="checkbox" checked={force} onChange={e => { setForce(e.target.checked); setArmed(0); }} />
            force (with inbox rows waiting, or an inbox that cannot be read)
          </label>
        )}
      </div>
      <MigMsg msg={msg} />
    </div>
  );
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
  // the panel shows what changes the cousin; the dialog and the check show their own
  const migOp = op && inList(state && state.op_kinds, op.kind)
    && op.kind !== "migrate-plan" && op.kind !== "migrate-check" ? op : null;
  const lane = state && state.lane;
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>kind and migration</SectionLabel>
      <div data-migrate-panel style={MIG_COL}>
        <MigLoadError error={loadError} />
        {!state && !loadError && <div style={MIG_HINT}>loading...</div>}
        {state && (
          <>
            <div style={MIG_MONO}>lane: <b>{lane}</b></div>
            <div style={MIG_HINT}>{recordLine("migration", state.migration)}</div>
            <div style={MIG_HINT}>{recordLine("kind switch", state.switch)}</div>
            <div style={MIG_ROW}>
              {state.refusal ? (
                <div data-migrate-refusal style={MIG_HINT}>{state.refusal}</div>
              ) : (
                <button className="btn" style={MIG_SMALL} disabled={running} data-open-kind-switch
                        onClick={() => openKindSwitchDialog(slug)}>switch kind...</button>
              )}
              <button className={`btn ${showCheck ? "active" : "ghost"}`} style={MIG_SMALL}
                      onClick={() => setShowCheck(!showCheck)}>check</button>
            </div>
            {!migOp && <PaneWait wait={paneWait(state, null)} slug={slug} onOpenPane={() => setPaneOpen(true)} />}
            {showCheck && <CheckReport slug={slug} />}
            {rollbackOffers(state).map(o => (
              <RollbackButton key={o.which} slug={slug} offer={o} disabled={running} onStarted={reloadState} />
            ))}
            {migOp && <MigrateOpStages op={migOp} state={state} onOpenPane={() => setPaneOpen(true)} />}
            <DeferredList items={state.deferred} />
          </>
        )}
      </div>
      {paneOpen && <MigratePaneModal slug={slug} onClose={() => setPaneOpen(false)} />}
    </>
  );
}

// ---- the inspector: lifecycle -------------------------------------------------

// GET /api/lifecycle/modes: [meta, error].
function useLifecycleModes() {
  const [modes, setModes] = React.useState(null);
  const [error, setError] = React.useState(null);
  React.useEffect(() => {
    migGet("/api/lifecycle/modes").then(res => { if (res.d) setModes(res.d); else setError(res.error); });
  }, []);
  return [modes, error];
}

// GET /api/cousins/<slug>/lifecycle: what holds the cousin as a
// transplant's donor, reloaded when a transplant op moves anywhere.
function useLifecycleHeld(slug) {
  const [held, setHeld] = React.useState(null);
  const [error, setError] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!slug) return;
    const res = await migGet(`/api/cousins/${slug}/lifecycle`);
    if (res.d) { setHeld(res.d.held || null); setError(null); } else setError(res.error);
  }, [slug]);
  React.useEffect(() => {
    load();
    const on = (e) => { if ((e.detail || {}).kind === "transplant") load(); };
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
  const [timeout, setTimeoutS] = React.useState("");
  const [armed, setArmed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [opId, setOpId] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const max = (meta && meta.role_max) || 200;
  const range = (meta && meta.timeout_range) || [10, 600];
  const clean = role.trim();
  const valid = clean && clean.length <= max && !/[\r\n]/.test(role);
  const start = async () => {
    if (!armed) { setArmed(true); return; }
    setArmed(false); setBusy(true); setMsg(null);
    const body = { new_role: clean, confirm: true };
    if (String(timeout).trim()) body.timeout = Number(timeout);
    const res = await migPost(`/api/cousins/${slug}/reincarnate`, body);
    setBusy(false);
    if (!res.ok) { setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` }); return; }
    setOpId(res.d.op.id);
  };
  const shown = op && (op.id === opId || (op.status === "running" && op.kind === "reincarnate")) ? op : null;
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" data-reincarnate={slug} onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>reincarnate {slug}</span>
          <span style={{ ...MIG_HINT, marginLeft: 8 }}>cousin-reincarnate</span>
          <button className="close" onClick={onClose} style={{ marginLeft: "auto" }}>x</button>
        </div>
        <div className="body" style={MIG_COL}>
          <div style={MIG_HINT}>
            The role changes, the memory stays. A snapshot of the continuity files first
            (data/lifecycle/{slug}/), then the bequest: the cousin is asked for data/handoff.md and the
            console waits for it (a runner cousin answers it on the flip's own handoff request), then the
            role is rewritten in CLAUDE.md and cousin.toml, then a flip.
          </div>
          <MigLoadError error={metaError} />
          <div style={MIG_MONO}>role now: {cousin.role || "-"}</div>
          <FormField label="new role" hint={`one line, at most ${max} characters`}>
            <input className="txt" value={role} maxLength={max} autoFocus onChange={e => { setRole(e.target.value); setArmed(false); }} />
          </FormField>
          <FormField label="bequest wait (seconds)" hint={`${range[0]} to ${range[1]}; default ${(meta && meta.timeout) || 300}`}>
            <input className="txt" type="number" value={timeout} min={range[0]} max={range[1]} style={{ width: 100 }}
                   onChange={e => setTimeoutS(e.target.value)} />
          </FormField>
          {shown && <MigrateOpStages op={shown} />}
          <MigMsg msg={msg} />
        </div>
        <div className="foot">
          <button className="btn" onClick={onClose}>close</button>
          <button className={`btn ${armed ? "danger" : "primary"}`} disabled={busy || !valid || (op && op.status === "running")} onClick={start}>
            {armed ? `click again: rewrite the role and flip ${slug}` : "reincarnate"}
          </button>
        </div>
      </div>
    </div>
  );
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
    migGet("/api/cousins").then(res => {
      if (!res.d) { setCousinsError(res.error); return; }
      setCousins((res.d.cousins || []).filter(c => c.type !== "remote").map(c => c.slug));
    });
  }, []);
  React.useEffect(() => { setArmed(false); setTyped(""); }, [donor, recipient, mode]);
  const modes = (meta && meta.modes) || [];
  const info = modes.find(m => m.id === mode);
  const typedMode = info && info.confirm === "typed";
  const phrase = typedMode ? confirmPhrase(info.phrase, donor, recipient) : "";
  const ready = donor && recipient && donor !== recipient && info;
  const start = async () => {
    if (!typedMode && !armed) { setArmed(true); return; }
    setArmed(false); setBusy(true); setMsg(null);
    const res = await migPost("/api/lifecycle/transplant",
                              { donor, recipient, mode, confirm: typedMode ? typed : true });
    setBusy(false);
    if (!res.ok) { setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` }); return; }
    setOpId(res.d.op.id);
  };
  const shown = op && (op.id === opId || (op.status === "running" && op.kind === "transplant")) ? op : null;
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" data-transplant onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>transplant</span>
          <span style={{ ...MIG_HINT, marginLeft: 8 }}>cousin-transplant</span>
          <button className="close" onClick={onClose} style={{ marginLeft: "auto" }}>x</button>
        </div>
        <div className="body" style={MIG_COL}>
          <MigLoadError error={metaError || cousinsError} />
          <div className="grid2">
            <FormField label="donor" hint="whose memory or body moves">
              <select className="sel" value={donor} onChange={e => setDonor(e.target.value)}>
                <option value="">(pick one)</option>
                {cousins.filter(s => s !== recipient).map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </FormField>
            <FormField label="recipient" hint="the op runs on it; the donor is held">
              <select className="sel" value={recipient} onChange={e => setRecipient(e.target.value)}>
                {cousins.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </FormField>
          </div>
          <FormField label="mode" hint={info ? info.what : ""}>
            <select className="sel" value={mode} onChange={e => setMode(e.target.value)}>
              {modes.map(m => <option key={m.id} value={m.id}>{m.id}{m.confirm === "typed" ? " (destructive)" : ""}</option>)}
            </select>
          </FormField>
          <div style={MIG_HINT}>
            Both cousins are snapshotted first (data/lifecycle/&lt;slug&gt;/), then the mode is applied, then
            both flip, the donor first. Nothing else starts on either while it runs.
          </div>
          {typedMode && ready && (
            <div style={MIG_ROW} data-transplant-typed>
              <span style={{ ...MIG_HINT, color: "var(--amber)" }}>{mode} replaces what {recipient} is: type <code>{phrase}</code> to confirm</span>
              <input className="txt" style={{ width: 200 }} value={typed} onChange={e => setTyped(e.target.value)} />
            </div>
          )}
          {shown && <MigrateOpStages op={shown} />}
          <MigMsg msg={msg} />
        </div>
        <div className="foot">
          <button className="btn" onClick={onClose}>close</button>
          <button className={`btn ${armed || typedMode ? "danger" : "primary"}`}
                  disabled={busy || !ready || (typedMode && (!phrase || typed !== phrase)) || (op && op.status === "running")}
                  onClick={start}>
            {armed ? `click again: ${mode} ${donor} into ${recipient}, then flip both` : "transplant"}
          </button>
        </div>
      </div>
    </div>
  );
}

function LifecyclePanel({ cousin }) {
  const [open, setOpen] = React.useState(null);
  const local = cousin.type !== "remote";
  const [op] = useLongOp(local ? cousin.slug : null);
  const [meta, metaError] = useLifecycleModes();
  const [held, heldError] = useLifecycleHeld(local ? cousin.slug : null);
  if (!local) return null;
  const lifeOp = op && inList(meta && meta.op_kinds, op.kind) ? op : null;
  const running = (op && op.status === "running") || !!held;
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>lifecycle</SectionLabel>
      <div data-lifecycle-panel style={MIG_COL}>
        <div style={MIG_ROW}>
          <button className="btn" style={MIG_SMALL} disabled={running} onClick={() => setOpen("reincarnate")}>reincarnate...</button>
          <button className="btn" style={MIG_SMALL} disabled={running} onClick={() => setOpen("transplant")}>transplant...</button>
          <span style={MIG_HINT}>a new role with the memory kept; or memory or body moved between two cousins</span>
        </div>
        <MigLoadError error={metaError || heldError} />
        {held && (
          <div data-lifecycle-held style={{ ...MIG_MONO, color: "var(--amber)" }}>
            held as the donor of a {held.mode} into {held.recipient}: nothing else starts on {cousin.slug}
            until it ends (its steps are on {held.recipient}'s inspector)
          </div>
        )}
        {lifeOp && <MigrateOpStages op={lifeOp} />}
      </div>
      {open === "reincarnate" && <ReincarnateDialog cousin={cousin} onClose={() => setOpen(null)} />}
      {open === "transplant" && <TransplantDialog cousin={cousin} onClose={() => setOpen(null)} />}
    </>
  );
}

registerSlot("inspector.panels", { id: "migrate", order: 40, render: ({ cousin }) => <MigratePanel cousin={cousin} /> });
registerSlot("inspector.panels", { id: "lifecycle", order: 41, render: ({ cousin }) => <LifecyclePanel cousin={cousin} /> });

// The dialog's host, mounted once beside the app (never inside a panel that
// closes with the inspector).
(function mountKindSwitchHost() {
  try {
    const host = document.createElement("div");
    host.setAttribute("data-kind-switch-host", "");
    document.body.appendChild(host);
    ReactDOM.createRoot(host).render(<KindSwitchHost />);
  } catch (e) {
    console.error("the kind switch dialog could not be mounted:", e);
  }
})();

Object.assign(window, {
  KindSwitchDialog, KindSwitchHost, openKindSwitchDialog, MigratePanel, LifecyclePanel,
  ReincarnateDialog, TransplantDialog, MigrateOpStages, MigratePaneModal,
  migrateStageRows, paneWaitScreen, paneAction, rollbackOffers, confirmPhrase,
});
