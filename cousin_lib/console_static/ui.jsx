// Shared UI primitives
const { useState, useEffect, useRef, useMemo, useCallback } = React;

function Led({ state, pulse }) {
  const map = {
    running: "green", healthy: "green", succeeded: "green", done: "green", ok: "green",
    waiting_tool: "cyan", waiting_user: "amber", queued: "amber", idle: "amber",
    degraded: "amber", paused: "amber", starting: "amber", stopping: "amber",
    failed: "red", down: "red", lost: "amber", cancelled: "gray", stopped: "gray", disabled: "gray",
  };
  const cls = map[state] || "gray";
  return <span className={`led ${cls}${pulse ? " pulse" : ""}`} />;
}

function Pill({ tone = "gray", children }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

function StatePill({ state }) {
  const label = String(state || "").replace("_", " ");
  const tone = {
    running: "green", healthy: "green", done: "green",
    succeeded: "cyan",
    waiting_tool: "cyan", waiting_user: "amber", queued: "amber", idle: "amber",
    degraded: "amber", starting: "amber", stopping: "amber",
    failed: "red", lost: "amber", cancelled: "gray", stopped: "gray", disabled: "gray",
  }[state] || "gray";
  return (
    <span className={`pill ${tone}`}>
      <Led state={state} pulse={state === "running" || state === "waiting_tool"} />
      {label}
    </span>
  );
}

function Bar({ pct, tone }) {
  const t = tone || (pct > 92 ? "crit" : pct > 75 ? "warn" : "");
  return (
    <div className={`bar ${t}`}>
      <span style={{ width: Math.min(100, pct) + "%" }} />
    </div>
  );
}

// Simple sparkline from a numeric array
function Spark({ data, width = 80, height = 20, tone }) {
  if (!data || data.length === 0) return null;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = Math.max(max - min, 1);
  const step = width / (data.length - 1 || 1);
  const pts = data.map((v, i) => {
    const x = i * step;
    const y = height - ((v - min) / range) * (height - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const d = "M" + pts.join(" L");
  return (
    <svg className={`spark ${tone || ""}`} width={width} height={height}>
      <path d={d} />
    </svg>
  );
}

// Small SVG icons (no emoji)
const I = {
  chat:     <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M2 3h12v9H6l-4 3z"/></svg>,
  cousins:  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="5" cy="6" r="2"/><circle cx="11" cy="6" r="2"/><path d="M2 14c0-2 1.5-3 3-3s3 1 3 3M8 14c0-2 1.5-3 3-3s3 1 3 3"/></svg>,
  list:     <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="3" width="12" height="2"/><rect x="2" y="7" width="12" height="2"/><rect x="2" y="11" width="12" height="2"/></svg>,
  memory:   <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="4" width="12" height="8" rx="1"/><path d="M5 4v8M8 4v8M11 4v8"/></svg>,
  loops:    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M13 8a5 5 0 1 1-1.5-3.5M13 3v3h-3"/></svg>,
  tokens:   <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="8" cy="8" r="6"/><path d="M6 6h2.5a1.5 1.5 0 0 1 0 3H6m0 0V12M6 9h4"/></svg>,
  tracker:  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3 4h2M7 4h6M3 8h2M7 8h6M3 12h2M7 12h6"/></svg>,
  host:     <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="3" width="12" height="4" rx="0.5"/><rect x="2" y="9" width="12" height="4" rx="0.5"/><circle cx="4" cy="5" r="0.5" fill="currentColor"/><circle cx="4" cy="11" r="0.5" fill="currentColor"/></svg>,
  meetings: <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="8" cy="9.5" r="2.5"/><circle cx="8" cy="3" r="1.3"/><circle cx="2.8" cy="13" r="1.3"/><circle cx="13.2" cy="13" r="1.3"/></svg>,
  logs:     <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3 2h7l3 3v9H3z"/><path d="M5 8h6M5 11h6M5 5h3"/></svg>,
  plus:     <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2"><path d="M8 3v10M3 8h10"/></svg>,
  play:     <svg viewBox="0 0 16 16" width="10" height="10" fill="currentColor"><path d="M4 3l9 5-9 5z"/></svg>,
  stop:     <svg viewBox="0 0 16 16" width="10" height="10" fill="currentColor"><rect x="4" y="4" width="8" height="8"/></svg>,
  kill:     <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 4l8 8M12 4l-8 8"/></svg>,
  eye:      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z"/><circle cx="8" cy="8" r="2"/></svg>,
  eyeOff:   <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z"/><circle cx="8" cy="8" r="2"/><path d="M2 2l12 12" strokeWidth="1.6"/></svg>,
};

// ECG-style heartbeat graph. Tile a fixed-width segment and translate by
// exactly one segment-width per cycle so the loop appears seamless.
//   state: "active"  -> sharp QRS, fast scroll
//          "idle"    -> small bump, slow scroll
//          "stopped" -> flatline, no animation
const HBG_SEG = 40;
const HeartbeatGraph = React.memo(function HeartbeatGraph({ state = "idle", width = 120, height = 24 }) {
  const mid = height / 2;
  // beat: relative moves summing to x=20, y=0. amp scales vertical spikes.
  const beat = (a) => `l2 0 l2 -${a} l2 ${3*a} l2 -${4*a} l2 ${3*a} l2 -${a} l4 0 l4 0`;
  const flat20 = "l20 0";
  let segment;
  if (state === "stopped") segment = `l${HBG_SEG} 0`;
  else if (state === "active") segment = `${flat20} ${beat(6)}`;
  else segment = `${flat20} ${beat(2)}`;
  // Tile segments to cover width + one extra so at the -SEG translate endpoint
  // the visual is identical to t=0.
  const reps = Math.ceil(width / HBG_SEG) + 2;
  const path = `M0 ${mid} ` + Array(reps).fill(segment).join(" ");
  return (
    <svg className={`hbg hbg-${state}`} width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <path d={path} />
    </svg>
  );
});

// ---- seam registry ------------------------------------------------------
// How a package adds UI without editing a shared file (the console UI
// parity work, WP0). Its own jsx file, listed in index.html's package
// block, calls these at load:
//   registerSlot("inspector.panels", { id: "mcp", order: 30,
//                render: ({ cousin }) => <McpPanel cousin={cousin} /> });
//   registerView({ id: "accounts", label: "Accounts", icon: I.host, order: 50,
//                  render: (props) => <AccountsView {...props} /> });
// Slots in use: inspector.lane (beside the lane rows), inspector.panels
// (with the other panels), inspector.actions (the button row), each with
// props { cousin }; settings.panels, props { auth, setAuth }. Entries
// render by `order` (default 100), an id registered again replaces the
// old entry, and each renders inside its own error boundary. A view gets a
// NAV entry after the built-in ones and props { cousins, sessionUser,
// onOpen }. Pure JS down to the end marker: the tests run it under node.
const RESERVED_VIEW_IDS = ["chat", "overview", "cousins", "jobs", "memory", "loops",
                           "tokens", "tracker", "meetings", "settings"];
const SEAM_SLOTS = {};
const SEAM_VIEWS = [];

function seamsChanged() {
  try { window.dispatchEvent(new CustomEvent("fw-seams-changed")); } catch (_e) { /* no DOM */ }
}

// `order`: absent is 100; anything that is not a finite number sorts last.
function seamOrder(order) {
  if (order === undefined) return 100;
  return typeof order === "number" && Number.isFinite(order) ? order : Infinity;
}
function seamCompare(a, b) {
  return a.order === b.order ? 0 : (a.order < b.order ? -1 : 1);
}

function registerSlot(name, entry) {
  if (typeof name !== "string" || !name) throw new Error("registerSlot: a slot name is required");
  if (!entry || typeof entry.id !== "string" || !entry.id || typeof entry.render !== "function") {
    throw new Error("registerSlot(" + name + "): an entry needs an id and a render function");
  }
  const list = SEAM_SLOTS[name] || (SEAM_SLOTS[name] = []);
  const row = Object.assign({}, entry, { order: seamOrder(entry.order) });
  const at = list.findIndex(e => e.id === row.id);
  if (at >= 0) list[at] = row; else list.push(row);
  list.sort(seamCompare);
  seamsChanged();
}

function slotEntries(name) {
  return (SEAM_SLOTS[name] || []).slice();
}

function registerView(entry) {
  if (!entry || typeof entry.id !== "string" || !/^[a-z][a-z0-9-]*$/.test(entry.id)
      || typeof entry.label !== "string" || typeof entry.render !== "function") {
    throw new Error("registerView: an entry needs an id, a label and a render function");
  }
  if (RESERVED_VIEW_IDS.includes(entry.id)) throw new Error("registerView: " + entry.id + " is a built-in view");
  const row = Object.assign({}, entry, { order: seamOrder(entry.order) });
  const at = SEAM_VIEWS.findIndex(v => v.id === row.id);
  if (at >= 0) SEAM_VIEWS[at] = row; else SEAM_VIEWS.push(row);
  SEAM_VIEWS.sort(seamCompare);
  seamsChanged();
}

function registeredViews() {
  return SEAM_VIEWS.slice();
}

function registeredView(id) {
  return SEAM_VIEWS.find(v => v.id === id) || null;
}
// ---- end seam registry --------------------------------------------------

// Re-render on a late registration (a package file that loaded after the
// first paint).
function useSeams() {
  const [, setTick] = React.useState(0);
  React.useEffect(() => {
    const on = () => setTick(t => t + 1);
    window.addEventListener("fw-seams-changed", on);
    return () => window.removeEventListener("fw-seams-changed", on);
  }, []);
}

class SlotBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error) { console.error("slot " + this.props.label + " failed:", error); }
  render() {
    if (this.state.error) {
      return <div className="slot-error">{this.props.label} failed: {String(this.state.error.message || this.state.error)}</div>;
    }
    return this.props.children;
  }
}

function SlotEntry({ entry, props }) {
  return entry.render(props) || null;
}

// <Slot name="inspector.panels" cousin={c} />: every entry a package
// registered for the name, in order, each in its own error boundary.
function Slot({ name, ...props }) {
  useSeams();
  const entries = slotEntries(name);
  if (!entries.length) return null;
  return (
    <>
      {entries.map(e => (
        // keyed by the cousin too: another cousin's inspector starts the
        // entry (and a boundary that caught an error) over
        <SlotBoundary key={e.id + "@" + ((props.cousin && props.cousin.slug) || "")} label={name + "/" + e.id}>
          <SlotEntry entry={e} props={props} />
        </SlotBoundary>
      ))}
    </>
  );
}

// ---- write-only secrets ---------------------------------------------------
// What may be shown about a stored secret: {set, last4} from the server
// (console/secrets.py secret_state), never the value.
function secretStateText(st) {
  if (!st || !st.set) return "not set";
  return st.last4 ? `set (ends ${st.last4})` : "set";
}

// A write-only secret box: a password input whose draft is cleared BEFORE
// onSubmit(value) is called, so the value is sent once and never kept or
// shown. Props: onSubmit (async; it reports its own errors), status
// ({set, last4}: shown as "set" or its last four, omit to show nothing),
// placeholder, submitLabel, busy, disabled, onCancel (adds a cancel
// button; Escape clears either way), autoFocus, hint.
function SecretField({ onSubmit, status, placeholder = "paste the secret", submitLabel = "save",
                       busy = false, disabled = false, onCancel, autoFocus = false, hint }) {
  const [draft, setDraft] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const blocked = busy || sending || disabled;
  const send = async () => {
    if (blocked || !draft.trim()) return;
    const value = draft.trim();
    setDraft("");
    setSending(true);
    try { await onSubmit(value); } finally { setSending(false); }
  };
  const cancel = () => { setDraft(""); if (onCancel) onCancel(); };
  return (
    <div className="secret-field">
      <div className="secret-row">
        <input className="txt" type="password" autoComplete="off" spellCheck={false}
               value={draft} placeholder={placeholder} autoFocus={autoFocus} disabled={disabled}
               style={{ flex: "1 1 180px", minWidth: 0 }}
               onChange={e => setDraft(e.target.value)}
               onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); send(); }
                                 if (e.key === "Escape") { e.stopPropagation(); cancel(); } }} />
        {onCancel && <button className="btn" style={small} disabled={sending} onClick={cancel}>cancel</button>}
        <button className="btn primary" style={small} disabled={blocked || !draft.trim()} onClick={send}>
          {submitLabel}
        </button>
      </div>
      {status !== undefined && <span className="secret-state">{secretStateText(status)}</span>}
      {hint && <span className="secret-hint">{hint}</span>}
    </div>
  );
}

// ---- phone layout -----------------------------------------------------------
// True on a phone: the same cut as styles.css's `@media (max-width: 820px)`,
// live as the window turns or resizes. A component that lays itself out
// differently on a phone (the chat header, the chat status line) branches
// on it and leaves its desktop markup as it was.
const MOBILE_QUERY = "(max-width: 820px)";
function useMobileLayout() {
  const read = () => typeof window !== "undefined" && !!window.matchMedia
    && window.matchMedia(MOBILE_QUERY).matches;
  const [mobile, setMobile] = React.useState(read);
  React.useEffect(() => {
    if (!window.matchMedia) return undefined;
    const mq = window.matchMedia(MOBILE_QUERY);
    const on = () => setMobile(mq.matches);
    on();
    if (mq.addEventListener) mq.addEventListener("change", on); else mq.addListener(on);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener("change", on); else mq.removeListener(on);
    };
  }, []);
  return mobile;
}

// True while an element is narrower than `px`, measured with a
// ResizeObserver: the chat header goes compact when its column is narrow
// (the reasoning pane beside it), not only on a phone. Returns
// [ref, narrow]; the ref is a callback, so an element that mounts later
// (after a loading branch) is still observed.
function useNarrowerThan(px) {
  const [narrow, setNarrow] = React.useState(false);
  const obsRef = React.useRef(null);
  const ref = React.useCallback((el) => {
    if (obsRef.current) { obsRef.current.disconnect(); obsRef.current = null; }
    if (!el) return;
    const read = (w) => setNarrow(w > 0 && w < px);
    read(el.getBoundingClientRect().width);
    if (!window.ResizeObserver) return;
    obsRef.current = new window.ResizeObserver(entries => {
      const e = entries[entries.length - 1];
      read(e.contentRect ? e.contentRect.width : el.getBoundingClientRect().width);
    });
    obsRef.current.observe(el);
  }, [px]);
  React.useEffect(() => () => { if (obsRef.current) obsRef.current.disconnect(); }, []);
  return [ref, narrow];
}

// ---- long operations --------------------------------------------------------
// A cousin's long operation (console/longop.py): [op, reload] from GET
// /api/cousins/<slug>/op, reloaded on each `cousin-op` event for the slug
// (app.jsx re-dispatches it as the window event "fw-cousin-op").
function useLongOp(slug) {
  const [op, setOp] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!slug) { setOp(null); return; }
    const d = await apiGet(`/api/cousins/${slug}/op`);
    if (d) setOp(d.op || null);
  }, [slug]);
  React.useEffect(() => {
    load();
    const on = (e) => { if ((e.detail || {}).slug === slug) load(); };
    window.addEventListener("fw-cousin-op", on);
    return () => window.removeEventListener("fw-cousin-op", on);
  }, [slug, load]);
  return [op, load];
}

// The op's stages as a list; nothing when there is no op (or `kind` is
// given and the op is another kind).
function LongOpStatus({ slug, kind }) {
  const [op] = useLongOp(slug);
  if (!op || (kind && op.kind !== kind)) return null;
  return (
    <div className="longop" data-longop={op.kind}>
      <div className="longop-hdr">
        <StatePill state={op.status} />
        <span className="mono">{op.kind}</span>
        {op.finished_at && <span className="muted">{fmtAgo(Date.now() / 1000 - op.finished_at)} ago</span>}
      </div>
      <ol className="longop-stages">
        {(op.stages || []).map(s => (
          <li key={s.name}>
            <Led state={s.status === "skipped" ? "disabled" : s.status} pulse={s.status === "running"} />
            <span>{s.name}</span>
            {s.detail && <span className="muted"> · {s.detail}</span>}
          </li>
        ))}
      </ol>
      {op.error && <div className="longop-error">{op.error}</div>}
    </div>
  );
}

Object.assign(window, {
  Led, Pill, StatePill, Bar, Spark, HeartbeatGraph, I,
  registerSlot, slotEntries, Slot, registerView, registeredViews, registeredView, RESERVED_VIEW_IDS,
  SlotBoundary, SlotEntry, SecretField, secretStateText, useLongOp, LongOpStatus,
  useMobileLayout, useNarrowerThan,
});
