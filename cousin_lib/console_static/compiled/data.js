const NOW = () => Date.now();
const COUSINS_SEED = [];
const LOOPS_SEED = [];
const MEMORY_SEED = {};
const HOST_SEED = {
  host: "",
  kernel: "",
  uptime: 0,
  cpu: { load1: 0, load5: 0, load15: 0, pct: 0 },
  mem: { total: 0, used: 0, cached: 0 },
  disk: { total: 0, used: 0 },
  net: { rx: 0, tx: 0 }
};
function noteAuthRequired(r) {
  if (r && r.status === 401) {
    try {
      window.dispatchEvent(new CustomEvent("console-auth-required"));
    } catch (_e) {
    }
  }
}
async function apiGet(path) {
  try {
    const r = await fetch(path, { cache: "no-store" });
    noteAuthRequired(r);
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return await r.json();
  } catch (e) {
    console.error("apiGet failed:", path, e);
    return null;
  }
}
async function apiSend(method, path, body) {
  const opts = { method, cache: "no-store" };
  if (body !== void 0) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  noteAuthRequired(r);
  let d = null;
  try {
    d = await r.json();
  } catch (_e) {
    d = { ok: false, error: `HTTP ${r.status}` };
  }
  return { r, d };
}
async function fetchCousins() {
  const d = await apiGet("/api/cousins");
  return d?.cousins ?? [];
}
async function fetchLoops() {
  const d = await apiGet("/api/loops");
  return d?.loops ?? [];
}
async function fetchLoopsFull() {
  const d = await apiGet("/api/loops");
  return d ?? { loops: [], daemon: null, errors: [] };
}
async function fetchTokens() {
  const d = await apiGet("/api/tokens");
  return d ?? { available: false, reason: "console unreachable", cousins: [] };
}
async function fetchHost() {
  return await apiGet("/api/host") ?? HOST_SEED;
}
async function fetchMemory() {
  const d = await apiGet("/api/memory");
  return d?.tree ?? {};
}
async function fetchAuthMe() {
  return await apiGet("/api/auth/me") ?? { user: null, configured: false, users: [] };
}
function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m " + sec % 60 + "s";
  if (sec < 86400) return Math.floor(sec / 3600) + "h " + Math.floor(sec % 3600 / 60) + "m";
  return Math.floor(sec / 86400) + "d " + Math.floor(sec % 86400 / 3600) + "h";
}
function fmtAgo(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 1) return "now";
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
  return Math.floor(sec / 86400) + "d ago";
}
function fmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtPct(n) {
  return (isFinite(n) ? n : 0).toFixed(1) + "%";
}
function chatSeenKey(slug, user) {
  return `console_chat_seen_${slug}_${user || "-"}`;
}
Object.assign(window, {
  COUSINS_SEED,
  LOOPS_SEED,
  MEMORY_SEED,
  HOST_SEED,
  fmtDuration,
  fmtAgo,
  fmtTokens,
  fmtPct,
  NOW,
  chatSeenKey,
  apiGet,
  apiSend,
  fetchCousins,
  fetchLoops,
  fetchLoopsFull,
  fetchTokens,
  fetchHost,
  fetchMemory,
  fetchAuthMe
});
