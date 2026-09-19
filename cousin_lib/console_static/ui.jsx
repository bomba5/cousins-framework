// Shared UI primitives
const { useState, useEffect, useRef, useMemo, useCallback } = React;

function Led({ state, pulse }) {
  const map = {
    running: "green", healthy: "green", succeeded: "green", done: "green", ok: "green",
    waiting_tool: "cyan", waiting_user: "amber", queued: "amber", idle: "amber",
    degraded: "amber", paused: "amber", starting: "amber", stopping: "amber",
    failed: "red", down: "red", cancelled: "gray", stopped: "gray", disabled: "gray",
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
    failed: "red", cancelled: "gray", stopped: "gray", disabled: "gray",
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

Object.assign(window, { Led, Pill, StatePill, Bar, Spark, HeartbeatGraph, I });
