// explorer.jsx: the per-cousin memory explorer (layers, raw entries,
// decisions, files, trash) and the cousin file explorer. Both read
// through confined routes (docs/reference/console-api.md, "Memory explorer" and
// "Cousin files"); nothing here decides what is safe to show.

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " kB";
  return n + " B";
}
function fmtWhen(epoch) {
  if (!epoch) return "-";
  return fmtAgo(Date.now() / 1000 - epoch);
}
// marked renders whatever HTML a file carries; a memory or note file is
// data, so the result is scrubbed before it reaches the DOM: no script,
// style, frames or embeds, no on* handlers, no javascript: links.
function sanitizeHtml(html) {
  try {
    const doc = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
    const root = doc.body.firstChild;
    root.querySelectorAll("script,style,iframe,object,embed,link,meta,base,form").forEach(n => n.remove());
    root.querySelectorAll("*").forEach(el => {
      for (const a of [...el.attributes]) {
        const name = a.name.toLowerCase();
        const val = (a.value || "").trim().toLowerCase();
        if (name.startsWith("on")) el.removeAttribute(a.name);
        else if ((name === "href" || name === "src" || name === "xlink:href") && (val.startsWith("javascript:") || val.startsWith("data:text"))) el.removeAttribute(a.name);
      }
      if (el.tagName === "A") { el.setAttribute("target", "_blank"); el.setAttribute("rel", "noopener noreferrer"); }
    });
    return root.innerHTML;
  } catch (_e) {
    return escapeHtml(html);
  }
}

function MarkdownDoc({ text }) {
  const html = React.useMemo(() => sanitizeHtml(renderMarkdown(text || "")), [text]);
  return <div className="chat-md mx-md" data-md-doc dangerouslySetInnerHTML={{ __html: html }} />;
}

function LinesView({ page }) {
  const start = page.start || 1;
  return (
    <pre className="mx-lines" data-lines-view>
      {(page.lines || []).map((l, i) => (
        <div key={start + i} className="mx-line">
          <span className="mx-ln">{start + i}</span>
          <span className="mx-lt">{l || " "}</span>
        </div>
      ))}
    </pre>
  );
}

// One file, any kind: Markdown rendered in full (with a raw toggle),
// text with line numbers paged by the server, images previewed through
// the download route, binaries only downloadable.
function FileViewer({ readUrl, downloadUrl, path, onDelete, deleteLabel }) {
  const [page, setPage] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [raw, setRaw] = React.useState(false);
  const PAGE = 1000;
  const load = React.useCallback(async (start) => {
    setErr(null);
    const sep = readUrl.includes("?") ? "&" : "?";
    try {
      const r = await fetch(`${readUrl}${sep}start=${start}&count=${PAGE}`, { cache: "no-store" });
      const d = await r.json();
      if (!r.ok) { setErr(d.error || `HTTP ${r.status}`); setPage(null); return; }
      setPage(d);
    } catch (e) { setErr(String(e.message || e)); }
  }, [readUrl]);
  React.useEffect(() => { setPage(null); setRaw(false); load(1); }, [load]);

  const head = (
    <div className="mx-filehdr">
      <span className="mx-path" title={path}>{path}</span>
      {page && <span className="muted">{fmtBytes(page.size)} · {fmtWhen(page.mtime)}</span>}
      <span style={{ flex: 1 }} />
      {page && page.kind === "markdown" && (
        <button className="btn ghost" onClick={() => setRaw(v => !v)}>{raw ? "rendered" : "raw"}</button>
      )}
      {downloadUrl && <a className="btn ghost" href={downloadUrl} download>download</a>}
      {onDelete && <button className="btn danger" onClick={onDelete}>{deleteLabel || "delete"}</button>}
    </div>
  );
  if (err) return <div className="mx-viewer">{head}<div className="mx-empty">{err}</div></div>;
  if (!page) return <div className="mx-viewer">{head}<div className="mx-empty">loading...</div></div>;
  let body;
  if (page.kind === "markdown") {
    body = raw ? <LinesView page={{ start: 1, lines: (page.text || "").split("\n") }} /> : <MarkdownDoc text={page.text} />;
  } else if (page.kind === "text") {
    const first = page.start, last = page.start + page.lines.length - 1;
    body = (
      <>
        {(page.start > 1 || page.more) && (
          <div className="mx-pager">
            lines {first}-{last} of {page.total_lines}
            <button className="btn ghost" disabled={page.start <= 1} onClick={() => load(Math.max(1, page.start - PAGE))}>prev</button>
            <button className="btn ghost" disabled={!page.more} onClick={() => load(page.start + PAGE)}>next</button>
            <button className="btn ghost" disabled={!page.more} onClick={() => load(Math.max(1, page.total_lines - PAGE + 1))}>end</button>
          </div>
        )}
        <LinesView page={page} />
      </>
    );
  } else if (page.kind === "image") {
    body = <div className="mx-img"><img src={downloadUrl} alt={path} /></div>;
  } else {
    body = <div className="mx-empty">binary file ({page.mime || "unknown type"}, {fmtBytes(page.size)}): not shown as text. Use download.</div>;
  }
  return <div className="mx-viewer">{head}<div className="mx-viewer-body">{body}</div></div>;
}

// ============ MEMORY EXPLORER ============
// ---- truth-level helpers (pure: no React, no fetch; tests run them in node) ----
const TRUTH_LEVELS = [
  "L0_OPERATOR", "L1_FRAMEWORK", "L2_TOOL",
  "L3_COUSIN_CONCLUSION", "L4_COUSIN_HYPOTHESIS", "L5_OBSOLETE",
];
// Each level's word, the tone it is drawn in, and what it means. The
// operator's level is the accent: it is the one the others answer to.
const LEVEL_META = {
  L0_OPERATOR:          { label: "operator",   tone: "cyan",   note: "what the operator said, with where" },
  L1_FRAMEWORK:         { label: "framework",  tone: "violet", note: "state changes the framework made" },
  L2_TOOL:              { label: "tool",       tone: "green",  note: "a measurement or a command's output" },
  L3_COUSIN_CONCLUSION: { label: "conclusion", tone: "blue",   note: "the cousin's own reasoning" },
  L4_COUSIN_HYPOTHESIS: { label: "hypothesis", tone: "amber",  note: "unverified" },
  L5_OBSOLETE:          { label: "obsolete",   tone: "gray",   note: "superseded: kept, struck through" },
  other:                { label: "other",      tone: "red",    note: "a level the framework does not know" },
};
const LEVEL_TONE = Object.fromEntries(Object.entries(LEVEL_META).map(([k, v]) => [k, v.tone]));
const LEVEL_SHORT = (l) => (l || "").replace(/^(L\d)_.*/, "$1") || "?";
const levelMeta = (l) => LEVEL_META[l] || LEVEL_META.other;

// Entries grouped by truth level, the levels in their order (operator
// first, obsolete last, an unknown level after them), each group's
// entries in the order they came (the route sends newest first). Empty
// levels are left out; nothing is dropped.
function groupByLevel(entries) {
  const order = TRUTH_LEVELS.concat(["other"]);
  const groups = {};
  for (const e of entries || []) {
    const lvl = TRUTH_LEVELS.includes(e.level) ? e.level : "other";
    (groups[lvl] = groups[lvl] || []).push(e);
  }
  return order.filter(l => groups[l]).map(l => ({ level: l, entries: groups[l] }));
}

// Where an operator-stated entry says it came from: the record's `cite`
// (cousin-memory remember/decide --cite), else null.
function entryCite(e) {
  const c = e ? (e.cite != null ? e.cite : e.extra && e.extra.cite) : null;
  return c == null || String(c).trim() === "" ? null : String(c);
}
// ---- end truth-level helpers ----

function fmtStamp(iso) {
  if (!iso) return "undated";
  return String(iso).replace("T", " ").slice(0, 16);
}

// Two-step confirm: the first click arms, the second (within 4 s) acts.
// `disabled` disarms it and refuses both clicks.
function ConfirmButton({ label, confirmLabel, onConfirm, className, title, disabled }) {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  React.useEffect(() => { if (disabled) setArmed(false); }, [disabled]);
  return (
    <button className={className || "btn danger"} title={title} disabled={!!disabled}
            data-confirm={armed ? "armed" : "idle"}
            onClick={(e) => { e.stopPropagation(); if (disabled) return; if (armed) { setArmed(false); onConfirm(); } else setArmed(true); }}>
      {armed ? (confirmLabel || "confirm: move to trash") : (label || "delete")}
    </button>
  );
}

function MemoryExplorer({ slug }) {
  const [ov, setOv] = React.useState(null);
  const [layer, setLayer] = React.useState("insights");
  const [toast, setToast] = React.useState(null);
  const [tick, setTick] = React.useState(0);
  // The truth-level filter lives here, not in the entry list: the rail
  // sets it from any layer and the raw, digest and archive lists read it.
  const [levels, setLevels] = React.useState([]);
  const [levelCounts, setLevelCounts] = React.useState(null);
  React.useEffect(() => { setLevels([]); setLevelCounts(null); }, [slug]);
  // The operator actions' rail badges (tensions, held entries) and the
  // topic the history panel opens on.
  const [badges, setBadges] = React.useState({});
  const [historyTopic, setHistoryTopic] = React.useState("");
  React.useEffect(() => { setHistoryTopic(""); }, [slug]);
  // Whether the viewer is this cousin's operator account: the retire
  // buttons on operator-level claims are theirs alone.
  const [writer] = useMemoryJson(`/api/memory/${slug}/writer`, 0);
  const isOperator = !!(writer && writer.can_write_operator);
  React.useEffect(() => {
    let cancelled = false;
    Promise.all([apiGet(`/api/memory/${slug}/tensions`), apiGet(`/api/memory/${slug}/review`)]).then(([t, r]) => {
      if (!cancelled) setBadges({ tensions: t ? t.tensions.length : null, held: r ? r.held.length : null });
    });
    return () => { cancelled = true; };
  }, [slug, tick]);

  const refresh = React.useCallback(async () => {
    const d = await apiGet(`/api/memory/${slug}/overview`);
    if (d) setOv(d);
  }, [slug]);
  React.useEffect(() => { setOv(null); setLayer("insights"); refresh(); }, [slug, refresh]);

  const flash = (t) => { setToast(t); setTimeout(() => setToast(x => (x === t ? null : x)), 8000); };

  // Every removal answers with the batch; the toast offers the undo.
  const remove = async (payload, what) => {
    const { r, d } = await apiSend("POST", `/api/memory/${slug}/delete`, payload);
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `could not remove ${what}: ${d.error || r.status}` }); return false; }
    flash({ ok: true, msg: `moved ${what} to trash${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}`, undo: d.trash.id });
    setTick(t => t + 1);
    refresh();
    return true;
  };
  // Mark a topic obsolete (L5): a new raw entry, never a removal. The
  // reason is required; the server records the logged-in user as `by`.
  const markObsolete = async (topic) => {
    const why = window.prompt(`Mark topic "${topic}" obsolete?\nIt leaves the distilled views; raw keeps its history, and a later entry on the topic revives it.\n\nWhy (what superseded it)?`, "");
    if (why === null) return false;
    if (!why.trim()) { flash({ ok: false, msg: "not marked: a reason is required" }); return false; }
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/obsolete`, { topic, why: why.trim() });
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `could not mark "${topic}" obsolete: ${d.error || r.status}` }); return false; }
    flash({ ok: true, msg: `marked "${topic}" obsolete${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}` });
    setTick(t => t + 1);
    refresh();
    return true;
  };
  // Retire one claim (an entry-level L5 mark), from tensions or history.
  const retireClaim = async (body) => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/obsolete`, body);
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `claim not retired: ${d.error || r.status}` }); return false; }
    flash({ ok: true, msg: `retired claim ${body.entry} of "${body.topic}"${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}` });
    setTick(t => t + 1);
    refresh();
    return true;
  };
  const openHistory = (topic) => { setHistoryTopic(topic || ""); setLayer("op-history"); };
  const changed = () => { setTick(t => t + 1); refresh(); };
  const restore = async (id) => {
    const { r, d } = await apiSend("POST", `/api/memory/${slug}/restore`, { id });
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `restore failed: ${d.error || r.status}` }); return; }
    flash({ ok: true, msg: `restored ${(d.restored.items || []).length} item(s)` });
    setTick(t => t + 1);
    refresh();
  };

  if (!ov) return <div className="mx-empty">loading @{slug} memory...</div>;
  const rawLayer = layer === "raw" || layer === "digest" || layer === "archive";
  // The rail counts what the list would show: the current tier under the
  // current filters while a raw list is open, else the live entries the
  // overview counted.
  const counts = (rawLayer && levelCounts) || (ov.insights && ov.insights.levels) || {};
  const countTotal = Object.values(counts).reduce((a, b) => a + (Number(b) || 0), 0);
  const pickLevel = (lvl) => {
    if (!rawLayer) setLayer("raw");
    if (lvl === null) { setLevels([]); return; }
    setLevels(ls => ls.includes(lvl) ? ls.filter(x => x !== lvl) : [...ls, lvl]);
  };
  const actions = [
    ["op-search", "search", "keyword and meaning"],
    ["op-write", "write", "remember, decide"],
    ["op-tensions", "tensions", badges.tensions == null ? "live claims that disagree" : `${badges.tensions} topics`],
    ["op-review", "review gate", badges.held == null ? "keep or drop held entries" : `${badges.held} held`],
    ["op-history", "history", "a topic's claims"],
    ["op-maintain", "maintenance", "distill, compact, reindex"],
    ["op-dreams", "dreaming", "background passes and what each changed"],
    ["op-portrait", "self-portrait", "diff, edit, commit"],
    ["op-moments", "capsules and callbacks", "read-only"],
  ];
  const layers = ov.layers || [];
  const byId = Object.fromEntries(layers.map(l => [l.id, l]));
  const groups = [
    ["now", ["active", "index"]],
    ["candidates", ["raw", "digest", "archive"]],
    ["durable", ["distilled", "decisions", "memory", "notes", "harness"]],
    ["machinery", ["search", "recall", "trash", "legacy"]],
  ];
  const layerLabel = (l) => {
    if (!l) return "";
    if (l.id === "harness" && !l.configured) return "not configured";
    if (l.id === "search") return l.fts && l.fts.exists ? `${l.count} files indexed${l.fts.stale ? " · stale" : ""}` : "no index yet";
    if (l.id === "recall") return `${l.count} recalls`;
    if (l.id === "distilled") return `${l.count} files${l.stubs ? ` · ${l.stubs} empty` : ""}`;
    if (l.id === "digest" && l.count) return `${l.count} topics · ${l.represents} entries`;
    if (l.id === "archive") return `${l.count} months · ${fmtBytes(l.bytes)}`;
    const unit = { raw: "entries", decisions: "decisions", trash: "batches" }[l.id] || "files";
    return `${l.count} ${unit}`;
  };

  return (
    <div className="mx-grid" data-memory-explorer={slug}>
      <div className="mx-side panel">
        <div className="panel-hdr"><span className="title">@{slug} memory</span></div>
        <div className="panel-body" style={{ padding: 0, overflowY: "auto", flex: 1 }}>
          <div className="mx-group">truth level</div>
          <div className="mx-levels" data-level-rail>
            <div className={"mx-lvl" + (rawLayer && !levels.length ? " sel" : "")} onClick={() => pickLevel(null)}
                 title="every level">
              <span className="mx-lvl-bar tone-all" />
              <span className="mx-lvl-n">all levels</span>
              <span className="mx-lvl-c">{countTotal}</span>
            </div>
            {TRUTH_LEVELS.map(l => (
              <div key={l} data-level={l}
                   className={"mx-lvl lvl-" + LEVEL_SHORT(l) + (levels.includes(l) ? " sel" : "") + (counts[l] ? "" : " dim")}
                   onClick={() => pickLevel(l)}
                   title={`${l}: ${levelMeta(l).note}. Click to filter; click again to clear.`}>
                <span className={"mx-lvl-bar tone-" + levelMeta(l).tone} />
                <span className="mx-lvl-n">{levelMeta(l).label}</span>
                <span className="mx-lvl-c">{counts[l] || 0}</span>
              </div>
            ))}
            {counts.other ? (
              <div className="mx-lvl dim" title={LEVEL_META.other.note}>
                <span className="mx-lvl-bar tone-red" />
                <span className="mx-lvl-n">other</span>
                <span className="mx-lvl-c">{counts.other}</span>
              </div>
            ) : null}
          </div>
          <div className="mx-group">layers</div>
          <div className={"mx-layer" + (layer === "insights" ? " sel" : "")} onClick={() => setLayer("insights")} data-layer="insights">
            <div className="mx-layer-t">insights</div>
            <div className="mx-layer-s">levels, recall, hygiene</div>
          </div>
          <div className="mx-group">operator</div>
          {actions.map(([id, title, sub]) => (
            <div key={id} data-layer={id} className={"mx-layer" + (layer === id ? " sel" : "")} onClick={() => setLayer(id)}>
              <div className="mx-layer-t">{title}</div>
              <div className="mx-layer-s">{sub}</div>
            </div>
          ))}
          {groups.map(([g, ids]) => (
            <div key={g}>
              <div className="mx-group">{g}</div>
              {ids.map(id => byId[id] && (
                <div key={id} data-layer={id}
                     className={"mx-layer" + (layer === id ? " sel" : "") + (byId[id].count ? "" : " dim")}
                     onClick={() => setLayer(id)}>
                  <div className="mx-layer-t">{byId[id].title}</div>
                  <div className="mx-layer-s">{layerLabel(byId[id])} · {fmtWhen(byId[id].updated)}</div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
      <div className="mx-main">
        {toast && (
          <div className={"mx-toast " + (toast.ok ? "ok" : "bad")}>
            {toast.msg}
            {toast.undo && <button className="btn ghost" onClick={() => { restore(toast.undo); setToast(null); }}>undo</button>}
          </div>
        )}
        {layer === "insights" && <MemoryInsights ov={ov} slug={slug} onPick={setLayer} onLevel={pickLevel} />}
        {rawLayer &&
          <RawEntries key={layer} slug={slug} tier={layer === "raw" ? "daily" : layer} reload={tick}
                      levels={levels} setLevels={setLevels} onCounts={setLevelCounts}
                      onRemove={remove} onObsolete={markObsolete} onHistory={openHistory} operator={isOperator} />}
        {layer === "op-search" && <MemorySearch slug={slug} onHistory={openHistory} />}
        {layer === "op-write" && <MemoryWrite slug={slug} flash={flash} onDone={changed} />}
        {layer === "op-tensions" && <TensionsList slug={slug} reload={tick} onRetire={retireClaim} onHistory={openHistory} operator={isOperator} />}
        {layer === "op-review" && <ReviewQueue slug={slug} reload={tick} flash={flash} onChanged={changed} />}
        {layer === "op-history" && <TopicHistory slug={slug} topic={historyTopic} reload={tick} onRetire={retireClaim} operator={isOperator} />}
        {layer === "op-maintain" && <MemoryMaintenance slug={slug} ov={ov} flash={flash} onChanged={changed} />}
        {layer === "op-dreams" && <DreamsView slug={slug} reload={tick} flash={flash} onChanged={changed} operator={isOperator} />}
        {layer === "op-portrait" && <SelfPortrait slug={slug} flash={flash} onChanged={changed} />}
        {layer === "op-moments" && <MomentsList slug={slug} reload={tick} />}
        {layer === "decisions" && <DecisionList slug={slug} reload={tick} onRemove={remove} />}
        {["active", "index", "distilled", "memory", "notes", "harness", "legacy"].includes(layer) &&
          <LayerFiles key={layer} slug={slug} layer={layer} info={byId[layer]} reload={tick} onRemove={remove} />}
        {layer === "trash" && <TrashList slug={slug} reload={tick} onRestore={restore} />}
        {layer === "search" && <SearchInfo l={byId.search} />}
        {layer === "recall" && <MemoryInsights ov={ov} slug={slug} onPick={setLayer} only="recall" />}
      </div>
    </div>
  );
}

function BarRow({ label, value, max, tone, onClick }) {
  const pct = max ? Math.round((value / max) * 100) : 0;
  return (
    <div className="mx-barrow" onClick={onClick} style={{ cursor: onClick ? "pointer" : "default" }}>
      <span className="mx-barlabel">{label}</span>
      <span className="mx-bar"><span className={"tone-" + (tone || "gray")} style={{ width: pct + "%" }} /></span>
      <span className="mx-barval">{value}</span>
    </div>
  );
}

function MemoryInsights({ ov, only, onLevel, onPick }) {
  const ins = ov.insights || {};
  const levels = ins.levels || {};
  const maxLevel = Math.max(1, ...Object.values(levels));
  const months = Object.entries(ins.per_month || {});
  const maxMonth = Math.max(1, ...months.map(([, v]) => v));
  const flags = [];
  if (ins.obsolete) flags.push(`${ins.obsolete} obsolete (L5) marks in raw: a topic whose newest entry is one stays out of the distilled views`);
  if (ins.hypotheses) flags.push(`${ins.hypotheses} hypotheses (L4) never confirmed or retired`);
  if (ins.pending_fold_files) flags.push(`${ins.pending_fold_files} daily raw files are past the fold window (cousin-memory compact --target raw folds them)`);
  if (ins.distilled_behind_raw) flags.push("raw has entries newer than the last distill (the loops daemon catches up within a tick; cousin-memory distill does it now)");
  if (ins.unparsable_lines) flags.push(`${ins.unparsable_lines} raw lines are not JSON and are skipped by every reader`);
  if (ins.undated) flags.push(`${ins.undated} raw entries carry no timestamp`);
  if ((ins.dangling_index_links || []).length) flags.push(`MEMORY.md points at ${ins.dangling_index_links.length} missing file(s): ${ins.dangling_index_links.join(", ")}`);
  const recall = (
    <div className="panel mx-card">
      <div className="panel-hdr"><span className="title">most recalled</span><span className="muted">{ins.recall_events || 0} recall events · last {fmtStamp(ins.last_recall)}</span></div>
      <div className="panel-body">
        {(ins.most_recalled || []).length === 0 && <div className="mx-empty">no recall recorded yet</div>}
        {(ins.most_recalled || []).map(r => (
          <BarRow key={r.path} label={r.path} value={r.count} max={(ins.most_recalled[0] || {}).count} tone="cyan" />
        ))}
        {(ins.cold_files || []).length > 0 && (
          <div className="mx-sub">never recalled: {ins.cold_files.join(", ")}</div>
        )}
      </div>
    </div>
  );
  if (only === "recall") return <div className="mx-cards">{recall}</div>;
  return (
    <div className="mx-cards" data-insights>
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">entries per truth level</span><span className="muted">{ins.topics || 0} topics · {ins.multi_entry_topics || 0} with history</span></div>
        <div className="panel-body">
          {Object.entries(levels).map(([lvl, n]) => (
            <BarRow key={lvl} label={`${LEVEL_SHORT(lvl)} ${levelMeta(lvl).label}`} value={n} max={maxLevel}
                    tone={levelMeta(lvl).tone}
                    onClick={onLevel && lvl !== "other" ? () => onLevel(lvl) : undefined} />
          ))}
        </div>
      </div>
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">hygiene</span></div>
        <div className="panel-body">
          {flags.length === 0 ? <div className="mx-empty">nothing to flag</div> :
            <ul className="mx-flags">{flags.map(f => <li key={f}>{f}</li>)}</ul>}
          {onPick && (ins.distilled_behind_raw || ins.pending_fold_files) ? (
            <button className="btn ghost" data-open-maintenance onClick={() => onPick("op-maintain")}>open maintenance</button>
          ) : null}
        </div>
      </div>
      {recall}
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">sources and busy topics</span></div>
        <div className="panel-body">
          {Object.entries(ins.sources || {}).map(([s, n]) => (
            <BarRow key={s} label={s} value={n} max={Math.max(...Object.values(ins.sources))} tone="violet" />
          ))}
          {(ins.top_topics || []).length > 0 && <div className="mx-sub">topics with the most entries:</div>}
          {(ins.top_topics || []).map(t => (
            <BarRow key={t.topic} label={t.topic} value={t.entries} max={ins.top_topics[0].entries} tone="amber" />
          ))}
        </div>
      </div>
      {months.length > 0 && (
        <div className="panel mx-card">
          <div className="panel-hdr"><span className="title">daily raw entries per month</span></div>
          <div className="panel-body">
            {months.map(([m, n]) => <BarRow key={m} label={m} value={n} max={maxMonth} tone="green" />)}
          </div>
        </div>
      )}
    </div>
  );
}

function SearchInfo({ l }) {
  if (!l) return null;
  return (
    <div className="mx-cards">
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">keyword index</span></div>
        <div className="panel-body mx-kv">
          {l.fts && l.fts.exists ? (
            <>
              <div>files indexed · <b>{l.fts.files}</b></div>
              <div>built · <b>{fmtWhen(l.fts.built_at)}</b></div>
              <div>state · <b>{l.fts.stale === null || l.fts.stale === undefined ? "unknown" : l.fts.stale ? "stale: rebuilds on the next search" : "current"}</b></div>
            </>
          ) : <div className="mx-empty">no keyword index yet; the first search builds it</div>}
        </div>
      </div>
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">semantic index</span></div>
        <div className="panel-body mx-kv">
          {l.embeddings && l.embeddings.exists ? (
            <>
              <div>size · <b>{fmtBytes(l.embeddings.size)}</b></div>
              {l.embeddings.chunks !== undefined && <div>chunks · <b>{l.embeddings.chunks}</b></div>}
            </>
          ) : <div className="mx-empty">no embeddings (semantic search not configured, or never run)</div>}
        </div>
      </div>
    </div>
  );
}

function RawEntries({ slug, tier, reload, onRemove, onObsolete, onHistory, operator, levels: levelsProp, setLevels: setLevelsProp, onCounts }) {
  // The level filter normally comes from the explorer's rail; alone, the
  // list keeps its own.
  const [ownLevels, setOwnLevels] = React.useState([]);
  const levels = levelsProp || ownLevels;
  const setLevels = setLevelsProp || setOwnLevels;
  const [topic, setTopic] = React.useState("");
  const [q, setQ] = React.useState("");
  const [since, setSince] = React.useState("");
  const [until, setUntil] = React.useState("");
  const [data, setData] = React.useState(null);
  const [limit, setLimit] = React.useState(100);
  const [grouping, setGrouping] = React.useState("level");  // level | time
  React.useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      const p = new URLSearchParams({ tier, limit: String(limit) });
      if (topic.trim()) p.set("topic", topic.trim());
      if (q.trim()) p.set("q", q.trim());
      if (since) p.set("since", since);
      if (until) p.set("until", until);
      // The rail's counts are every level under the other filters: with a
      // level chosen, one more call without it, cut to a single row.
      const all = new URLSearchParams(p);
      all.set("limit", "1");
      if (levels.length) p.set("level", levels.join(","));
      const [d, whole] = await Promise.all([
        apiGet(`/api/memory/${slug}/raw?` + p.toString()),
        levels.length ? apiGet(`/api/memory/${slug}/raw?` + all.toString()) : Promise.resolve(null),
      ]);
      if (cancelled) return;
      const got = d || { entries: [], total: 0, facets: {} };
      setData(got);
      if (onCounts) onCounts(((whole || got).facets || {}).levels || {});
    }, 200);
    return () => { cancelled = true; clearTimeout(t); };
  }, [slug, tier, levels.join(","), topic, q, since, until, limit, reload]);
  const facets = (data && data.facets && data.facets.levels) || {};

  const renderEntry = (e) => {
    const meta = levelMeta(e.level);
    const cite = entryCite(e);
    const extra = Object.entries(e.extra || {}).filter(([k]) => !(k === "cite" && e.level === "L0_OPERATOR"));
    return (
      <div key={(e.ref ? e.ref.path + ":" + e.ref.line_no : e.file + e.timestamp + e.topic)}
           className={"mx-entry lvl-" + LEVEL_SHORT(e.level)} data-entry-level={e.level}>
        <div className="mx-entry-h">
          <span className={"pill " + meta.tone} title={e.truth_level || "(no level stored: default)"}>{LEVEL_SHORT(e.level)} {meta.label}</span>
          <span className="mx-topic">{e.topic || <i>no topic</i>}</span>
          <span style={{ flex: 1 }} />
          <span className="muted">{fmtStamp(e.timestamp)}</span>
        </div>
        <div className="mx-content">{e.content}</div>
        {e.level === "L0_OPERATOR" && (
          <div className={"mx-cite" + (cite ? "" : " missing")} data-cite>
            {cite ? <>cited · <b>{cite}</b></> : "no citation stored"}
          </div>
        )}
        <div className="mx-meta">
          {e.source && <span>source · <b>{e.source}</b></span>}
          {e.tier === "digest" && <span>folded · <b>{e.entries}</b> entries {e.first_at} to {e.last_at}</span>}
          {extra.map(([k, v]) => <span key={k}>{k} · <b>{typeof v === "object" ? JSON.stringify(v) : String(v)}</b></span>)}
          <span className="muted">{e.file}{e.ref ? ":" + e.ref.line_no : ""}</span>
          <span style={{ flex: 1 }} />
          {e.topic && onHistory &&
            <button className="btn ghost" data-history={e.topic} title="the topic's claims with their valid time"
                    onClick={(ev) => { ev.stopPropagation(); onHistory(e.topic); }}>history</button>}
          {e.topic && e.level !== "L5_OBSOLETE" && onObsolete && (e.level !== "L0_OPERATOR" || operator) &&
            <button className="btn ghost" data-mark-obsolete={e.topic}
                    title="mark this topic superseded (L5): out of the distilled views, history kept in raw"
                    onClick={(ev) => { ev.stopPropagation(); onObsolete(e.topic); }}>mark obsolete</button>}
          {e.ref && <ConfirmButton className="btn ghost danger-text" label="remove"
                                   onConfirm={() => onRemove({ kind: "entry", ...e.ref }, `entry "${e.topic}"`)} />}
        </div>
      </div>
    );
  };

  return (
    <div className="mx-list-wrap">
      <div className="mx-filters" data-raw-filters>
        <div className="mx-inputs">
          <input placeholder="topic" value={topic} onChange={e => setTopic(e.target.value)} />
          <input placeholder="text" value={q} onChange={e => setQ(e.target.value)} />
          <label>from <input type="date" value={since} onChange={e => setSince(e.target.value)} /></label>
          <label>to <input type="date" value={until} onChange={e => setUntil(e.target.value)} /></label>
          <span style={{ flex: 1 }} />
          <div className="radio-row" data-grouping={grouping}>
            <button className={grouping === "level" ? "sel" : ""} onClick={() => setGrouping("level")}>by level</button>
            <button className={grouping === "time" ? "sel" : ""} onClick={() => setGrouping("time")}>newest first</button>
          </div>
        </div>
        <div className="muted mx-count">
          {data ? `${data.total} ${tier} entries match` : "loading..."}
          {levels.length > 0 && <> · levels: {levels.map(l => levelMeta(l).label).join(", ")} <button className="btn ghost mx-clear" onClick={() => setLevels([])}>clear</button></>}
          {data && Object.keys(facets).length > 0 && " · " + Object.entries(facets).map(([k, v]) => `${LEVEL_SHORT(k)} ${v}`).join(" · ")}
          {tier === "archive" && " · the archive is the forensic tier: read-only"}
        </div>
      </div>
      <div className="mx-list" data-raw-list>
        {data && data.entries.length === 0 && <div className="mx-empty">no entries</div>}
        {data && grouping === "level" && groupByLevel(data.entries).map(g => (
          <section key={g.level} className={"mx-lgroup lvl-" + LEVEL_SHORT(g.level)} data-level-group={g.level}>
            <div className="mx-lgroup-h">
              <span className={"mx-lvl-bar tone-" + levelMeta(g.level).tone} />
              <span className="mx-lgroup-t">{levelMeta(g.level).label}</span>
              <span className="mx-lgroup-c">{g.entries.length}{facets[g.level] > g.entries.length ? ` of ${facets[g.level]}` : ""}</span>
              <span className="mx-lgroup-note">{levelMeta(g.level).note}</span>
            </div>
            {g.entries.map(renderEntry)}
          </section>
        ))}
        {data && grouping === "time" && data.entries.map(renderEntry)}
        {data && data.total > data.entries.length && (
          <button className="btn" onClick={() => setLimit(l => Math.min(1000, l + 200))}>show more ({data.total - data.entries.length} left)</button>
        )}
      </div>
    </div>
  );
}

function DecisionList({ slug, reload, onRemove }) {
  const [q, setQ] = React.useState("");
  const [data, setData] = React.useState(null);
  const [limit, setLimit] = React.useState(100);
  const [withMirrors, setWithMirrors] = React.useState(true);
  React.useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      const p = new URLSearchParams({ limit: String(limit) });
      if (q.trim()) p.set("q", q.trim());
      const d = await apiGet(`/api/memory/${slug}/decisions?` + p.toString());
      if (!cancelled) setData(d || { entries: [], total: 0 });
    }, 200);
    return () => { cancelled = true; clearTimeout(t); };
  }, [slug, q, limit, reload]);
  return (
    <div className="mx-list-wrap">
      <div className="mx-filters">
        <div className="mx-inputs">
          <input placeholder="search topic, decision, reasoning" value={q} onChange={e => setQ(e.target.value)} style={{ flex: 1 }} />
          <label title="decide also wrote each decision into memory/raw; remove that copy too">
            <input type="checkbox" checked={withMirrors} onChange={e => setWithMirrors(e.target.checked)} /> remove the raw copy too
          </label>
        </div>
        <div className="muted mx-count">{data ? `${data.total} decisions` : "loading..."}</div>
      </div>
      <div className="mx-list">
        {data && data.entries.length === 0 && <div className="mx-empty">no decisions</div>}
        {data && data.entries.map(d => (
          <div key={d.ref ? d.ref.line_no + d.ref.sha : d.file + d.timestamp} className="mx-entry mx-decision">
            <div className="mx-entry-h">
              <span className="mx-topic">{d.topic}</span>
              <span style={{ flex: 1 }} />
              <span className="muted">{fmtStamp(d.timestamp)}</span>
            </div>
            <div className="mx-content"><b>decided:</b> {d.decision}</div>
            <div className="mx-content mx-why"><b>why:</b> {d.reasoning}</div>
            <div className="mx-meta">
              <span className="muted">{d.mirrors.length ? `${d.mirrors.length} raw copy` : "no raw copy"}</span>
              <span style={{ flex: 1 }} />
              {d.ref && <ConfirmButton className="btn ghost danger-text" label="remove"
                                       onConfirm={() => onRemove({ kind: "decision", ...d.ref, mirrors: withMirrors }, `decision "${d.topic}"`)} />}
            </div>
          </div>
        ))}
        {data && data.total > data.entries.length && (
          <button className="btn" onClick={() => setLimit(l => Math.min(1000, l + 200))}>show more</button>
        )}
      </div>
    </div>
  );
}

function LayerFiles({ slug, layer, info, reload, onRemove }) {
  const [files, setFiles] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet(`/api/memory/${slug}/files?layer=${layer}`).then(d => {
      if (cancelled) return;
      const list = (d && d.files) || [];
      setFiles(list);
      setOpen(cur => (cur && list.some(f => f.path === cur)) ? cur : (list.length ? list[0].path : null));
    });
    return () => { cancelled = true; };
  }, [slug, layer, reload]);
  const extra = layer === "harness" ? "&layer=harness" : "";
  const cur = files && files.find(f => f.path === open);
  const hint = {
    distilled: "generated from raw by the distiller: remove the raw entries, not these files",
    active: "session state the boot packet reads first; edited by the cousin, read-only here",
    index: "the pointer index; read-only here",
    harness: info && info.directory ? `the harness's own auto-memory: ${info.directory} (read-only here)` : "no harness auto-memory directory configured",
    legacy: "the pre-migration archive: removal asks twice and still goes to trash",
  }[layer];
  return (
    <div className="mx-files">
      <div className="mx-filelist panel">
        {hint && <div className="mx-hint">{hint}</div>}
        {files && files.length === 0 && <div className="mx-empty">no files</div>}
        {(files || []).map(f => (
          <div key={f.path} data-file={f.path} className={"mx-file" + (open === f.path ? " sel" : "")} onClick={() => setOpen(f.path)}>
            <div className="mx-file-n">{f.path}{f.stub ? <span className="pill gray" style={{ marginLeft: 6 }}>empty</span> : null}</div>
            <div className="mx-file-s">{fmtBytes(f.size)} · {fmtWhen(f.mtime)}{f.recalls ? ` · recalled ${f.recalls}x` : ""}</div>
          </div>
        ))}
      </div>
      <div className="mx-filepane">
        {cur ? (
          <FileViewer key={cur.path}
            readUrl={`/api/memory/${slug}/file?path=${encodeURIComponent(cur.path)}${extra}`}
            downloadUrl={layer === "harness" ? null : `/api/cousins/${slug}/files/download?path=${encodeURIComponent(cur.path)}`}
            path={cur.path}
            deleteLabel={cur.legacy ? "remove legacy file" : "remove"}
            onDelete={cur.deletable ? () => {
              const msg = cur.legacy
                ? `${cur.path} is part of the legacy archive. Move it to the trash anyway?`
                : `Move ${cur.path} to the trash? It can be restored from the trash layer.`;
              if (!window.confirm(msg)) return;
              onRemove({ kind: "file", path: cur.path, legacy: !!cur.legacy }, cur.path).then(ok => { if (ok) setOpen(null); });
            } : null} />
        ) : <div className="mx-empty">select a file</div>}
      </div>
    </div>
  );
}

function TrashList({ slug, reload, onRestore }) {
  const [batches, setBatches] = React.useState(null);
  React.useEffect(() => {
    apiGet(`/api/memory/${slug}/trash`).then(d => setBatches((d && d.batches) || []));
  }, [slug, reload]);
  return (
    <div className="mx-list-wrap">
      <div className="mx-hint">removed memories are kept here under memory/.trash/ with their original path; restore puts them back (also: cousin-memory trash restore ID)</div>
      <div className="mx-list" data-trash-list>
        {batches && batches.length === 0 && <div className="mx-empty">trash is empty</div>}
        {(batches || []).map(b => (
          <div key={b.id} className="mx-entry" data-trash-id={b.id}>
            <div className="mx-entry-h">
              <span className="mx-topic">{b.id}</span>
              <span className="muted">by {b.by || "-"} · {fmtStamp(b.deleted_at)}</span>
              <span style={{ flex: 1 }} />
              <button className="btn" onClick={() => onRestore(b.id)}>restore</button>
            </div>
            {(b.items || []).map((it, i) => (
              <div key={i} className="mx-content">
                {it.kind === "file" ? <>file <b>{it.path}</b> ({fmtBytes(it.size)})</> :
                  <><span className="muted">{it.path}:{it.line_no}</span> <TrashLine line={it.line} /></>}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function TrashLine({ line }) {
  let e = null;
  try { e = JSON.parse(line); } catch (_e) { /* not JSON */ }
  if (!e || typeof e !== "object") return <code>{line}</code>;
  return <span><b>{e.topic || "-"}</b>: {e.content || e.decision || ""}</span>;
}

// ============ MEMORY OPERATOR ACTIONS ============
// Search, the write forms, history, tensions, the review gate, the
// maintenance runs, the self-portrait gate, callbacks and capsules, and
// the shared tier's reviewer list (docs/reference/console-api.md,
// "Memory operator actions"). The server fills every cite and decides
// who may write what; these views only say so ahead of time.

// ---- operator-action helpers (pure: no React, no fetch; tests run them in node) ----
const SEARCH_LEG_LABEL = { keyword: "keyword", semantic: "meaning" };
// How a hit was found: "keyword", "meaning" or "keyword + meaning".
function hitLegsText(legs) {
  const words = (legs || []).map(l => SEARCH_LEG_LABEL[l] || l);
  return words.length ? words.join(" + ") : "fused";
}
// The review route's body: only real verdicts (keep or drop) travel; an
// id left undecided stays held.
function reviewBody(marks, why) {
  const verdicts = {};
  for (const [id, v] of Object.entries(marks || {})) {
    if (v === "keep" || v === "drop") verdicts[id] = v;
  }
  return { verdicts, why: String(why || "").trim() };
}
// A retirement needs its reason; null when there is none.
function claimRetireBody(topic, id, why) {
  const w = String(why || "").trim();
  if (!topic || !id || !w) return null;
  return { topic, why: w, entry: id };
}
// The identity gate's last step: the slug typed back exactly, a
// candidate hash to pin, and no unsaved edit (the server commits what is
// on disk, not what is in the box).
function portraitCommitReady(typed, slug, sha, dirty) {
  return !!slug && typed === slug && !!sha && !dirty;
}
// ---- end operator-action helpers ----

// apiSend (the shared helper: a 401 brings the login back) that never
// throws: a network failure comes back as a failed answer, so every
// caller can clear its busy state and say why.
async function safeSend(method, path, body) {
  try {
    return await apiSend(method, path, body);
  } catch (e) {
    return { r: { ok: false, status: 0 }, d: { ok: false, error: `network: ${e.message || e}` } };
  }
}

// [data, reload, error]: a GET that says why it failed instead of
// leaving "loading..." up forever.
function useMemoryJson(url, reload) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!url) { setData(null); setErr(null); return; }
    const { r, d } = await safeSend("GET", url);
    if (!r.ok) { setErr(d.error || `HTTP ${r.status}`); return; }
    setErr(null);
    setData(d);
  }, [url]);
  React.useEffect(() => { load(); }, [load, reload]);
  return [data, load, err];
}

// "loading..." until the data comes, or the error that stopped it.
function Pending({ data, err }) {
  if (err) return <div className="mx-toast bad">{err}</div>;
  if (!data) return <div className="mx-empty">loading...</div>;
  return null;
}

function LevelPill({ level }) {
  const meta = levelMeta(level);
  return <span className={"pill " + meta.tone} title={level}>{LEVEL_SHORT(level)} {meta.label}</span>;
}

// "retire this claim": an entry-level obsolete mark, with its reason.
function ClaimRetire({ topic, id, onRetire }) {
  const [open, setOpen] = React.useState(false);
  const [why, setWhy] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  if (!open) {
    return <button className="btn ghost" data-retire-claim={id}
                   title="an entry-level obsolete mark: this claim leaves the views, the topic stays, raw keeps it"
                   onClick={(e) => { e.stopPropagation(); setOpen(true); }}>retire this claim</button>;
  }
  const body = claimRetireBody(topic, id, why);
  const go = async () => {
    if (!body || busy) return;
    setBusy(true);
    const ok = await onRetire(body);
    setBusy(false);
    if (ok) { setOpen(false); setWhy(""); }
  };
  return (
    <span className="mx-inputs" style={{ flex: "1 1 260px" }}>
      <input placeholder="why (what superseded it)" value={why} autoFocus style={{ flex: 1 }}
             onChange={e => setWhy(e.target.value)}
             onKeyDown={e => { if (e.key === "Enter") go(); if (e.key === "Escape") setOpen(false); }} />
      <button className="btn danger" disabled={!body || busy} onClick={go}>retire</button>
      <button className="btn ghost" onClick={() => { setOpen(false); setWhy(""); }}>cancel</button>
    </span>
  );
}

// `operator`: the viewer is the operator account; an operator-level claim
// is theirs alone to retire (the server refuses anyone else).
function ClaimRow({ c, topic, onRetire, showTopic, operator }) {
  const live = !c.valid_to;
  const theirs = normLevel(c.truth_level) === "L0_OPERATOR" && !operator;
  return (
    <div className={"mx-entry lvl-" + LEVEL_SHORT(normLevel(c.truth_level))} data-claim={c.id}
         data-entry-level={live ? normLevel(c.truth_level) : "L5_OBSOLETE"}>
      <div className="mx-entry-h">
        <LevelPill level={normLevel(c.truth_level)} />
        {showTopic && <span className="mx-topic">{c.topic}</span>}
        <span className="muted">{c.id}</span>
        <span style={{ flex: 1 }} />
        <span className="muted">{fmtStamp(c.valid_from)}</span>
      </div>
      <div className="mx-content">{c.content}</div>
      <div className="mx-meta">
        {c.source && <span>source · <b>{c.source}</b></span>}
        {c.cite && <span>cite · <b>{c.cite}</b></span>}
        <span>{live ? <b>live</b> : <>valid to <b>{fmtStamp(c.valid_to)}</b>{c.retired_by ? ` (mark ${c.retired_by})` : ""}</>}</span>
        <span style={{ flex: 1 }} />
        {live && onRetire && !theirs && <ClaimRetire topic={topic || c.topic} id={c.id} onRetire={onRetire} />}
        {live && onRetire && theirs && <span className="muted" data-operator-only>the operator's to retire</span>}
      </div>
    </div>
  );
}

// A raw entry's stored level in the canonical form the explorer draws.
function normLevel(l) {
  if (!l) return "L3_COUSIN_CONCLUSION";
  const up = String(l).toUpperCase();
  if (TRUTH_LEVELS.includes(up)) return up;
  const short = { "OPERATOR": "L0_OPERATOR", "OPERATOR-STATED": "L0_OPERATOR", "FRAMEWORK": "L1_FRAMEWORK",
                  "TOOL": "L2_TOOL", "CONCLUSION": "L3_COUSIN_CONCLUSION", "HYPOTHESIS": "L4_COUSIN_HYPOTHESIS",
                  "OBSOLETE": "L5_OBSOLETE" };
  return short[up] || "other";
}

function TensionsList({ slug, reload, onRetire, onHistory, operator }) {
  const [data, , err] = useMemoryJson(`/api/memory/${slug}/tensions`, reload);
  const list = (data && data.tensions) || null;
  return (
    <div className="mx-list-wrap" data-tensions>
      <div className="mx-hint">topics whose live claims disagree. Nothing judges which is right: settle one by retiring the claim that no longer holds (an entry-level obsolete mark; raw keeps it).</div>
      <div className="mx-list">
        <Pending data={list} err={err} />
        {list && list.length === 0 && <div className="mx-empty">no tensions</div>}
        {(list || []).map(t => (
          <section key={t.topic} className="mx-lgroup" data-tension={t.topic}>
            <div className="mx-lgroup-h">
              <span className="mx-lgroup-t">{t.topic}</span>
              <span className="mx-lgroup-c">{t.claims.length} live claims</span>
              <span style={{ flex: 1 }} />
              <button className="btn ghost" onClick={() => onHistory(t.topic)}>history</button>
            </div>
            {t.claims.map(c => <ClaimRow key={c.id} c={c} topic={t.topic} onRetire={onRetire} operator={operator} />)}
          </section>
        ))}
      </div>
    </div>
  );
}

function TopicHistory({ slug, topic: initial, reload, onRetire, operator }) {
  const [topic, setTopic] = React.useState(initial || "");
  const [asked, setAsked] = React.useState(initial || "");
  React.useEffect(() => { setTopic(initial || ""); setAsked(initial || ""); }, [initial]);
  const [data, , err] = useMemoryJson(asked ? `/api/memory/${slug}/history?topic=${encodeURIComponent(asked)}` : null, reload);
  return (
    <div className="mx-list-wrap" data-topic-history>
      <div className="mx-filters">
        <div className="mx-inputs">
          <input placeholder="topic (exact)" value={topic} style={{ flex: 1 }}
                 onChange={e => setTopic(e.target.value)}
                 onKeyDown={e => { if (e.key === "Enter") setAsked(topic.trim()); }} />
          <button className="btn" disabled={!topic.trim()} onClick={() => setAsked(topic.trim())}>show history</button>
        </div>
        <div className="muted mx-count">a topic's claims, oldest first, each with its id and valid time (cousin-memory history)</div>
      </div>
      <div className="mx-list">
        {!asked && <div className="mx-empty">type a topic</div>}
        {asked && <Pending data={data} err={err} />}
        {data && data.claims.length === 0 && <div className="mx-empty">no claims for "{asked}"</div>}
        {data && data.claims.map(c => <ClaimRow key={c.id} c={c} topic={asked} onRetire={onRetire} operator={operator} />)}
      </div>
    </div>
  );
}

function MemorySearch({ slug, onHistory }) {
  const [q, setQ] = React.useState("");
  const [collection, setCollection] = React.useState("");
  const [top, setTop] = React.useState(10);
  const [res, setRes] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [open, setOpen] = React.useState(null);
  React.useEffect(() => { setRes(null); setOpen(null); }, [slug]);
  const run = async () => {
    if (!q.trim() || busy) return;
    setBusy(true); setErr(null); setOpen(null);
    const p = new URLSearchParams({ q: q.trim(), top: String(top) });
    if (collection) p.set("collection", collection);
    const { r, d } = await safeSend("GET", `/api/memory/${slug}/search?` + p.toString());
    if (!r.ok) { setErr(d.error || `HTTP ${r.status}`); setRes(null); }
    else setRes(d);
    setBusy(false);
  };
  const semantic = res && { on: "on: ranked by meaning too", off: "off: config/embedding.toml is not set, keyword only",
                            broken: "promised but unusable: keyword only" }[res.semantic];
  return (
    <div className="mx-list-wrap" data-memory-search>
      <div className="mx-filters">
        <div className="mx-inputs">
          <input placeholder="search memory, notes, raw entries" value={q} autoFocus style={{ flex: 1 }}
                 onChange={e => setQ(e.target.value)} onKeyDown={e => { if (e.key === "Enter") run(); }} />
          <select className="sel-inline" value={collection} onChange={e => setCollection(e.target.value)}>
            <option value="">everything</option>
            <option value="memory">memory files</option>
            <option value="notes">notes</option>
            <option value="raw">raw entries</option>
            <option value="harness">harness auto-memory</option>
          </select>
          <select className="sel-inline" value={top} onChange={e => setTop(Number(e.target.value))}>
            {[5, 10, 20, 50].map(n => <option key={n} value={n}>top {n}</option>)}
          </select>
          <button className="btn primary" disabled={!q.trim() || busy} onClick={run}>{busy ? "searching..." : "search"}</button>
        </div>
        <div className="muted mx-count">
          the library search the cousin uses (cousin-memory search): keyword always, meaning when configured; a console search is not recorded as the cousin's recall
          {res && <> · semantic {semantic}</>}
        </div>
        {res && res.notice && <div className="mx-toast bad">{res.notice}</div>}
        {err && <div className="mx-toast bad">{err}</div>}
      </div>
      <div className="mx-list">
        {res && res.hits.length === 0 && <div className="mx-empty">no matches</div>}
        {res && res.hits.map((h, i) => (
          <div key={h.rel + i} className="mx-entry" data-search-hit={h.collection}>
            <div className="mx-entry-h">
              <span className="muted">{i + 1}.</span>
              <span className={"pill " + (h.legs.includes("semantic") ? "violet" : "blue")} data-legs={h.legs.join(",")}>{hitLegsText(h.legs)}</span>
              <span className="pill gray">{h.collection}</span>
              {h.entry && <LevelPill level={h.entry.level} />}
              <span className="mx-topic">{h.entry ? h.entry.topic : h.rel}</span>
              <span style={{ flex: 1 }} />
              {h.similarity != null && <span className="muted">similarity {Number(h.similarity).toFixed(3)}</span>}
            </div>
            <div className="mx-content">{h.entry ? h.entry.content : h.snippet}</div>
            <div className="mx-meta">
              <span className="muted">{h.rel}</span>
              {h.entry && h.entry.timestamp && <span className="muted">{fmtStamp(h.entry.timestamp)}</span>}
              {h.entry && h.entry.cite && <span>cite · <b>{h.entry.cite}</b></span>}
              <span style={{ flex: 1 }} />
              {h.entry && h.entry.topic && <button className="btn ghost" onClick={() => onHistory(h.entry.topic)}>history</button>}
              {!h.entry && <button className="btn ghost" onClick={() => setOpen(open === h.rel ? null : h.rel)}>{open === h.rel ? "close" : "open"}</button>}
            </div>
            {open === h.rel && !h.entry && (
              <div style={{ display: "flex", minHeight: 240, maxHeight: "60vh" }}>
                <FileViewer readUrl={`/api/memory/${slug}/file?path=${encodeURIComponent(h.rel)}${h.layer === "harness" ? "&layer=harness" : ""}`}
                            path={h.rel} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function MemoryWrite({ slug, flash, onDone }) {
  const [writer, , writerErr] = useMemoryJson(`/api/memory/${slug}/writer`, 0);
  const [kind, setKind] = React.useState("remember");
  const [form, setForm] = React.useState({ topic: "", fact: "", decision: "", reasoning: "", level: "conclusion", note: "" });
  const [busy, setBusy] = React.useState(false);
  const set = (k) => (e) => setForm(f => ({ ...f, [k]: e.target.value }));
  const levels = (writer && writer.levels) || ["conclusion"];
  React.useEffect(() => {
    if (writer && !levels.includes(form.level)) setForm(f => ({ ...f, level: "conclusion" }));
  }, [writer]);
  const ready = form.topic.trim() && (kind === "remember" ? form.fact.trim() : form.decision.trim() && form.reasoning.trim());
  const submit = async () => {
    if (!ready || busy) return;
    setBusy(true);
    const body = kind === "remember"
      ? { topic: form.topic, fact: form.fact, level: form.level, note: form.note }
      : { topic: form.topic, decision: form.decision, reasoning: form.reasoning, level: form.level, note: form.note };
    const url = kind === "remember" ? `/api/memory/${slug}/remember` : `/api/memory/${slug}/decide`;
    const { r, d } = await safeSend("POST", url, body);
    setBusy(false);
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `not written: ${d.error || r.status}` }); return; }
    flash({ ok: true, msg: d.line || "written" });
    setForm(f => ({ ...f, fact: "", decision: "", reasoning: "", note: "" }));
    onDone && onDone();
  };
  const who = writer ? (writer.user ? `console user ${writer.user}` : "console (no login)") : "...";
  return (
    <div className="mx-cards" data-memory-write>
      <div className="panel mx-card" style={{ gridColumn: "1 / -1" }}>
        <div className="panel-hdr">
          <span className="title">write to @{slug}'s memory</span>
          {writerErr && <span className="muted">{writerErr}</span>}
          <span style={{ flex: 1 }} />
          <div className="radio-row" data-write-kind={kind}>
            <button className={kind === "remember" ? "sel" : ""} onClick={() => setKind("remember")}>remember</button>
            <button className={kind === "decide" ? "sel" : ""} onClick={() => setKind("decide")}>decide</button>
          </div>
        </div>
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div className="field"><label>topic</label>
            <input className="txt" value={form.topic} onChange={set("topic")} placeholder="one topic per fact, as the cousin files them" /></div>
          {kind === "remember" ? (
            <div className="field"><label>fact</label>
              <textarea className="txt" value={form.fact} onChange={set("fact")} /></div>
          ) : (
            <>
              <div className="field"><label>decision</label>
                <textarea className="txt" value={form.decision} onChange={set("decision")} /></div>
              <div className="field"><label>why</label>
                <textarea className="txt" value={form.reasoning} onChange={set("reasoning")} /></div>
            </>
          )}
          <div className="field"><label>truth level</label>
            <select className="sel" value={form.level} onChange={set("level")} data-write-levels={levels.join(",")}>
              {levels.map(l => <option key={l} value={l}>{l}</option>)}
            </select>
            <span className="hint">
              {writer && writer.can_write_operator
                ? "operator: you are this cousin's operator, so you may state it as the operator's word"
                : `operator level is the operator account's only (${(writer && writer.operator) || "no [operator] name set"}); framework entries are the framework's, and obsolete is the retire action`}
            </span>
          </div>
          <div className="field"><label>note for the cite (optional)</label>
            <input className="txt" value={form.note} onChange={set("note")} placeholder="where it came from: a chat, a call, a document" maxLength={300} />
            <span className="hint" data-cite-preview>cite, filled by the console: <b>{who}, &lt;the time of writing&gt;{form.note.trim() ? "; " + form.note.trim() : ""}</b></span>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <span style={{ flex: 1 }} />
            <button className="btn primary" disabled={!ready || busy} onClick={submit}>{kind === "remember" ? "remember" : "log the decision"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ReviewQueue({ slug, reload, flash, onChanged }) {
  const [data, load, err] = useMemoryJson(`/api/memory/${slug}/review`, reload);
  const [writer] = useMemoryJson(`/api/memory/${slug}/writer`, 0);
  const [op] = useLongOp(slug);
  const [marks, setMarks] = React.useState({});
  const [why, setWhy] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => { setMarks({}); }, [slug, reload]);
  // A settle of more than a couple of entries runs as the cousin's long
  // operation: follow it, then report and reload when it ends.
  const reviewOp = op && op.kind === "memory-review" ? op : null;
  const running = !!(op && op.status === "running");
  const seen = React.useRef(null);
  React.useEffect(() => {
    if (!reviewOp) return;
    const key = reviewOp.id + ":" + reviewOp.status;
    if (seen.current && seen.current !== key && reviewOp.status !== "running") {
      if (reviewOp.status === "done" && reviewOp.result) report(reviewOp.result);
      else flash({ ok: false, msg: `review failed: ${reviewOp.error || "see the console log"}` });
      load(); onChanged && onChanged();
    }
    seen.current = key;
  }, [reviewOp && reviewOp.id, reviewOp && reviewOp.status]);
  const held = (data && data.held) || [];
  const body = reviewBody(marks, why);
  const n = Object.keys(body.verdicts).length;
  const drops = Object.values(body.verdicts).filter(v => v === "drop").length;
  const markAll = (v) => setMarks(Object.fromEntries(held.map(r => [r.id, v])));
  const report = (d) => {
    const errs = Object.entries(d.errors || {});
    flash({ ok: errs.length === 0, msg: `${Object.keys(d.done || {}).length} settled` + (errs.length ? ` · left held: ${errs.map(([k, v]) => `${k} (${v})`).join("; ")}` : "") });
  };
  const apply = async () => {
    if (!n || busy || running) return;
    setBusy(true);
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/review`, body);
    setBusy(false);
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `review not applied: ${d.error || r.status}` }); return; }
    setMarks({}); setWhy("");
    if (r.status === 202) { flash({ ok: true, msg: `settling ${n} as @${slug}'s long operation...` }); seen.current = "pending"; return; }
    report(d);
    load(); onChanged && onChanged();
  };
  const loggedIn = writer && writer.user;
  const blocked = busy || running || !loggedIn;
  return (
    <div className="mx-list-wrap" data-review-queue>
      <div className="mx-hint">
        the review gate holds a batch of new entries (over [memory] review_batch = {data ? data.batch : "?"} at once) out of every view until someone keeps or drops each. A drop is an entry-level obsolete mark and has no undo; an operator-level entry is dropped by the operator account only ({(data && data.operator) || "none set"}).
        {writer && !loggedIn && <> <b>A verdict needs a logged-in console user</b>: without logins the console cannot tell you from a cousin.</>}
      </div>
      {reviewOp && <LongOpStatus slug={slug} kind="memory-review" />}
      <div className="mx-inputs">
        <button className="btn ghost" disabled={!held.length || running} onClick={() => markAll("keep")}>mark all keep</button>
        <button className="btn ghost" disabled={!held.length || running} onClick={() => markAll("drop")}>mark all drop</button>
        <button className="btn ghost" disabled={!n} onClick={() => setMarks({})}>clear</button>
        <input placeholder="why (optional, recorded with each verdict)" value={why} maxLength={500} onChange={e => setWhy(e.target.value)} style={{ flex: 1 }} />
        {drops > 0
          ? <ConfirmButton className="btn danger" disabled={blocked} label={`apply ${n} (${drops} drop)`} confirmLabel={`confirm: drop ${drops}, no undo`} onConfirm={apply} />
          : <button className="btn primary" disabled={!n || blocked} onClick={apply}>apply {n || ""}</button>}
      </div>
      <div className="mx-list">
        <Pending data={data} err={err} />
        {data && held.length === 0 && <div className="mx-empty">nothing held for review</div>}
        {held.map(r => (
          <div key={r.id} className={"mx-entry lvl-" + LEVEL_SHORT(normLevel(r.truth_level))} data-held={r.id}>
            <div className="mx-entry-h">
              <LevelPill level={normLevel(r.truth_level)} />
              <span className="mx-topic">{r.topic}</span>
              <span className="muted">{r.id}</span>
              <span style={{ flex: 1 }} />
              <span className="muted">{fmtStamp(r.valid_from)}</span>
            </div>
            <div className="mx-content">{r.content}</div>
            <div className="mx-meta">
              {r.source && <span>source · <b>{r.source}</b></span>}
              {r.cite && <span>cite · <b>{r.cite}</b></span>}
              <span style={{ flex: 1 }} />
              <div className="radio-row" data-verdict={marks[r.id] || "held"}>
                {["keep", "drop"].map(v => (
                  <button key={v} className={marks[r.id] === v ? "sel" : ""} disabled={running}
                          onClick={() => setMarks(m => ({ ...m, [r.id]: m[r.id] === v ? undefined : v }))}>{v}</button>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const MAINTENANCE = [
  { action: "distill", title: "distill", what: "rebuild memory/distilled/ from raw (cousin-memory distill). The boot packet reads it; the loops daemon also catches up within a tick." },
  { action: "compact-raw", title: "compact raw", what: "fold daily raw files older than the hot window into monthly gzip archives and a per-topic digest (cousin-memory compact --target raw). Lossless." },
  { action: "compact-index", title: "compact the index", what: "retire the oldest MEMORY.md pointers until it fits its byte budget (cousin-memory compact). A snapshot is kept; preview first." },
  { action: "reindex", title: "reindex", what: "rebuild the keyword index, and the semantic one when config/embedding.toml sets it up (cousin-memory reindex). Embedding can take a while." },
];

function MemoryMaintenance({ slug, ov, flash, onChanged }) {
  const [op, reload] = useLongOp(slug);
  const mine = op && String(op.kind || "").startsWith("memory-") ? op : null;
  const running = op && op.status === "running";
  const last = React.useRef(null);
  React.useEffect(() => {
    if (!mine) return;
    const key = mine.id + ":" + mine.status;
    if (last.current && last.current !== key && mine.status !== "running") onChanged && onChanged();
    last.current = key;
  }, [mine && mine.id, mine && mine.status]);
  const start = async (action, extra) => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/maintain`, { action, ...(extra || {}) });
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `${action} not started: ${d.error || r.status}` }); return; }
    reload();
  };
  const ins = (ov && ov.insights) || {};
  const state = {
    distill: ins.distilled_behind_raw ? "raw has entries newer than the last distill" : "current",
    "compact-raw": ins.pending_fold_files ? `${ins.pending_fold_files} daily file(s) past the fold window` : "nothing past the fold window",
    "compact-index": "",
    reindex: "",
  };
  const result = mine && mine.result;
  return (
    <div className="mx-cards" data-memory-maintenance>
      {mine && (
        <div className="panel mx-card" style={{ gridColumn: "1 / -1" }}>
          <div className="panel-hdr"><span className="title">last run</span></div>
          <div className="panel-body">
            <LongOpStatus slug={slug} kind={mine.kind} />
            {result && result.report && result.report.would_retire && (
              <div className="mx-sub">{mine.params && mine.params.dry_run ? "would retire" : "retired"}: {(mine.params && mine.params.dry_run ? result.report.would_retire : result.report.retired).join(", ") || "nothing (the index fits its budget)"}</div>
            )}
          </div>
        </div>
      )}
      {op && running && !mine && <div className="mx-toast bad" style={{ gridColumn: "1 / -1" }}>a {op.kind} is running on @{slug}; maintenance waits for it</div>}
      {MAINTENANCE.map(m => (
        <div key={m.action} className="panel mx-card" data-maintain={m.action}>
          <div className="panel-hdr"><span className="title">{m.title}</span>{state[m.action] && <span className="muted">{state[m.action]}</span>}</div>
          <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="mx-sub" style={{ marginTop: 0 }}>{m.what}</div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              {m.action === "compact-index" ? (
                <>
                  <button className="btn" disabled={running} onClick={() => start(m.action, { dry_run: true })}>preview</button>
                  <ConfirmButton className="btn danger" label="compact" confirmLabel="confirm: retire pointers"
                                 disabled={running} onConfirm={() => start(m.action)} />
                </>
              ) : (
                <button className="btn primary" disabled={running} onClick={() => start(m.action)}>{m.title} now</button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// Dreaming (cousin_lib/dreaming.py): the cousin's setting, a pass on
// demand, and every pass newest first with what it changed. A pass is
// reversible: undo moves the lines it wrote (claims and marks) to the
// trash, so its retired claims are live again; a lost pass reads its
// changes from its journal.
const DREAM_RESULT_TONE = { done: "ok", no_change: "", budget: "warn", error: "bad", lost: "bad", running: "" };

function DreamsView({ slug, reload, flash, onChanged, operator }) {
  const [tick, setTick] = React.useState(0);
  const [data, , err] = useMemoryJson(`/api/memory/${slug}/dreams`, reload + tick);
  const [op, reloadOp] = useLongOp(slug);
  const mine = op && op.kind === "memory-dream" ? op : null;
  const running = op && op.status === "running";
  const last = React.useRef(null);
  React.useEffect(() => {
    if (!mine) return;
    const key = mine.id + ":" + mine.status;
    if (last.current && last.current !== key && mine.status !== "running") { setTick(t => t + 1); onChanged && onChanged(); }
    last.current = key;
  }, [mine && mine.id, mine && mine.status]);
  if (!data) return <Pending data={data} err={err} />;
  const cfg = data.settings || {};
  const run = async () => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/dreams/run`, {});
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `dreaming not started: ${d.error || r.status}` }); return; }
    reloadOp();
  };
  const undo = async (passId) => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/dreams/undo`, { pass_id: passId });
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `undo failed: ${d.error || r.status}` }); return; }
    flash({ ok: true, msg: `pass ${passId} undone: ${(d.reverted || []).length} change(s) reversed` });
    setTick(t => t + 1); onChanged && onChanged();
  };
  const passes = data.passes || [];
  return (
    <div className="mx-list-wrap" data-dreams>
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">setting</span>
          <span className="muted">{cfg.mode === "off" ? "off" : cfg.mode === "nightly" ? `nightly at ${cfg.at}` : "after each rollover"}</span></div>
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div className="mx-sub" style={{ marginTop: 0 }}>
            A pass is a short Sonnet session with memory tools only (no files, no shell), up to 64k tokens; the first pass starts 30 days back.
            It merges duplicates, retires stale claims and settles contradictions, never touching L0-L2.
            Turn it on or off in the cousin's Agent settings (dreaming, dreaming_at).
          </div>
          {mine && <LongOpStatus slug={slug} kind="memory-dream" />}
          {operator && (
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button className="btn primary" disabled={running} onClick={run}>dream now</button>
            </div>
          )}
        </div>
      </div>
      {passes.length === 0 && <div className="mx-empty">no passes yet</div>}
      {passes.map(p => (
        <div key={p.pass_id} className="panel mx-card" data-dream={p.pass_id}>
          <div className="panel-hdr">
            <span className="title">{fmtWhen(p.started)} · {p.trigger}</span>
            <span className={"muted " + (DREAM_RESULT_TONE[p.result] || "")}>
              {p.result}{p.undone ? " · undone" : ""} · {(p.changes || []).length} change(s){p.tokens != null ? ` · ${p.tokens} tokens` : ""}
            </span>
          </div>
          <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {p.summary && <div className="mx-sub" style={{ marginTop: 0 }}>{p.summary}</div>}
            {p.error && <div className="mx-sub bad" style={{ marginTop: 0 }}>{p.error}</div>}
            {(p.changes || []).map((c, i) => (
              <div key={i} className="mx-sub" style={{ marginTop: 0, fontFamily: "var(--mono)" }}>
                {c.op} · {c.topic || "-"}{c.why ? ` · ${c.why}` : ""}{(c.entry_ids || []).length ? ` · ${(c.entry_ids || []).join(", ")}` : ""}
              </div>
            ))}
            {operator && (p.changes || []).length > 0 && !p.undone && (
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <ConfirmButton className="btn danger" label="undo" confirmLabel="confirm: reverse this pass"
                               onConfirm={() => undo(p.pass_id)} />
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function SelfPortrait({ slug, flash, onChanged }) {
  const [st, load, err] = useMemoryJson(`/api/memory/${slug}/portrait`, 0);
  const [tab, setTab] = React.useState("diff");
  const [draft, setDraft] = React.useState(null);
  const [typed, setTyped] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => { setDraft(null); setTyped(""); }, [slug]);
  React.useEffect(() => { if (st && draft === null) setDraft(st.candidate || ""); }, [st]);
  const dirty = !!st && draft !== null && draft !== (st.candidate || "");
  const urls = {
    synthesize: `/api/memory/${slug}/portrait/synthesize`,
    candidate: `/api/memory/${slug}/portrait/candidate`,
    commit: `/api/memory/${slug}/portrait/commit`,
  };
  const call = async (what, body, okMsg) => {
    setBusy(true);
    const { r, d } = await safeSend("POST", urls[what], body || {});
    setBusy(false);
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `${what}: ${d.error || r.status}` }); if (r.status === 409) load(); return null; }
    flash({ ok: true, msg: okMsg });
    setDraft(d.candidate || "");
    await load();
    onChanged && onChanged();
    return d;
  };
  if (!st) return <Pending data={st} err={err} />;
  const ready = portraitCommitReady(typed, slug, st.candidate_sha, dirty);
  return (
    <div className="mx-list-wrap" data-self-portrait>
      <div className="mx-hint">
        the self-portrait is a reviewed identity layer: the boot packet reads only the committed one. Draft a candidate from the cousin's own sources, edit it, then commit. Committing is an identity gate: a logged-in person types the slug back; the previous portrait is kept as .self-portrait.md.bak.
      </div>
      <div className="mx-inputs">
        <div className="radio-row" data-portrait-tab={tab}>
          {["diff", "candidate", "committed"].map(t => <button key={t} className={tab === t ? "sel" : ""} onClick={() => setTab(t)}>{t}</button>)}
        </div>
        <span className="muted">candidate {st.candidate_exists ? st.candidate_sha : "none"} · committed {st.committed_exists ? "yes" : "none"}</span>
        <span style={{ flex: 1 }} />
        {st.candidate_exists
          ? <ConfirmButton className="btn" label="draft again" confirmLabel="confirm: replace the candidate" disabled={busy}
                           onConfirm={() => call("synthesize", { replace: true }, "candidate drafted again")} />
          : <button className="btn" disabled={busy} onClick={() => call("synthesize", {}, "candidate drafted")}>draft a candidate</button>}
      </div>
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "auto" }}>
        {tab === "diff" && (st.candidate_exists
          ? <pre className="mx-lines" data-portrait-diff style={{ padding: 12, whiteSpace: "pre-wrap" }}>{st.diff || "(no difference from the committed portrait)"}</pre>
          : <div className="mx-empty">no candidate: draft one, or write one in the candidate tab</div>)}
        {tab === "committed" && (st.committed_exists ? <div style={{ padding: 12 }}><MarkdownDoc text={st.committed} /></div>
                                                     : <div className="mx-empty">no committed self-portrait yet</div>)}
        {tab === "candidate" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: 1 }}>
            <textarea className="txt code" data-portrait-editor value={draft || ""} onChange={e => setDraft(e.target.value)}
                      style={{ flex: 1, minHeight: 320 }} spellCheck={false} />
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className="muted">{dirty ? "unsaved edits" : "saved"}</span>
              <span style={{ flex: 1 }} />
              <button className="btn" disabled={!dirty || busy} onClick={() => setDraft(st.candidate || "")}>discard edits</button>
              <button className="btn primary" disabled={!dirty || busy || !(draft || "").trim()}
                      onClick={() => call("candidate", { text: draft }, "candidate saved")}>save the candidate</button>
            </div>
          </div>
        )}
      </div>
      <div className="panel" data-portrait-commit>
        <div className="panel-body mx-inputs">
          <span>type <b>{slug}</b> to commit candidate {st.candidate_sha || "-"}</span>
          <input value={typed} onChange={e => setTyped(e.target.value)} placeholder={slug} autoComplete="off" spellCheck={false} />
          <button className="btn danger" disabled={!ready || busy}
                  onClick={async () => { if (await call("commit", { confirm: typed, sha: st.candidate_sha }, "self-portrait committed")) setTyped(""); }}>commit</button>
          {dirty && <span className="muted">save or discard the edits first</span>}
        </div>
      </div>
    </div>
  );
}

function MomentsList({ slug, reload }) {
  const [cb, , cbErr] = useMemoryJson(`/api/memory/${slug}/callbacks`, reload);
  const [cap, , capErr] = useMemoryJson(`/api/memory/${slug}/capsules`, reload);
  const list = (v) => Array.isArray(v) ? v : (v == null || v === "" ? [] : [String(v)]);
  return (
    <div className="mx-cards" data-memory-moments>
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">reasoning capsules</span><span className="muted">newest first · read-only (cousin-reason)</span></div>
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Pending data={cap} err={capErr} />
          {cap && cap.capsules.length === 0 && <div className="mx-empty">no capsules</div>}
          {(cap ? cap.capsules : []).map(c => (
            <div key={c.id} className="mx-entry" data-capsule={c.id}>
              <div className="mx-entry-h">
                <span className="mx-topic">{c.conclusion}</span>
                <span style={{ flex: 1 }} />
                <span className="pill gray">{c.confidence}</span>
              </div>
              {list(c.evidence).length > 0 && <div className="mx-content"><b>evidence:</b> {list(c.evidence).join(" · ")}</div>}
              {list(c.rejected).length > 0 && <div className="mx-content mx-why"><b>rejected:</b> {list(c.rejected).join(" · ")}</div>}
              <div className="mx-meta">{c.topic && <span>topic · <b>{c.topic}</b></span>}<span className="muted">{fmtStamp(c.timestamp)}</span></div>
            </div>
          ))}
        </div>
      </div>
      <div className="panel mx-card">
        <div className="panel-hdr"><span className="title">callbacks</span><span className="muted">newest first · read-only (cousin-callback)</span></div>
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <Pending data={cb} err={cbErr} />
          {cb && cb.callbacks.length === 0 && <div className="mx-empty">no callbacks</div>}
          {(cb ? cb.callbacks : []).map((m, i) => (
            <div key={i} className="mx-meta" data-callback>
              <span className="muted">{fmtStamp(m.time)}</span>
              {m.category && <span className="pill gray">{m.category}</span>}
              <span className="mx-content">{m.moment}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// The shared tier's reviewer list (config/shared-reviewers.json), shown
// beside the review queue in the memory view's shared scope. Changing it
// needs a logged-in user and a second click: it decides who may promote.
function SharedReviewersPanel({ onChange }) {
  const [st, setSt] = React.useState(null);
  const [edit, setEdit] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const load = React.useCallback(async () => {
    const { r, d } = await safeSend("GET", "/api/shared/reviewers");
    if (!r.ok) { setMsg({ ok: false, msg: `reviewers: ${d.error || r.status}` }); return; }
    setSt(d);
    if (onChange) onChange(d);
  }, [onChange]);
  React.useEffect(() => { load(); }, [load]);
  const save = async () => {
    const reviewers = (edit || "").split("\n").map(s => s.trim()).filter(Boolean);
    const { r, d } = await safeSend("POST", "/api/shared/reviewers", { reviewers });
    if (!r.ok || !d.ok) { setMsg({ ok: false, msg: d.error || `HTTP ${r.status}` }); return; }
    setMsg({ ok: true, msg: `reviewers saved: ${d.reviewers.join(", ") || "none"}` });
    setEdit(null);
    load();
  };
  if (!st) return msg ? <div className="mx-toast bad">{msg.msg}</div> : null;
  return (
    <div className="panel" style={{ flex: "0 0 auto" }} data-shared-reviewers>
      <div className="panel-hdr"><span className="title">reviewers</span>
        <span className="muted">{st.configured ? st.reviewers.length : "not configured"}</span>
        <span style={{ flex: 1 }} />
        {edit === null && <button className="btn ghost" disabled={!st.can_edit}
                                  title={st.can_edit ? "change who may promote into the shared tier"
                                         : st.user ? "only a current reviewer changes the list" : "needs a logged-in console user"}
                                  onClick={() => { setEdit(st.reviewers.join("\n")); setMsg(null); }}>edit</button>}
      </div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {st.error && <div className="mx-toast bad">config/shared-reviewers.json is {st.error}; fix it by hand</div>}
        {edit === null ? (
          <div className="mx-meta">
            {st.reviewers.map(r => <span key={r} className="pill gray">{r}</span>)}
            {st.reviewers.length === 0 && <span className="muted">nobody may promote yet</span>}
            {st.user && st.you_review !== null && <span className="muted">you ({st.user}) {st.you_review ? "review" : "do not review"}</span>}
          </div>
        ) : (
          <>
            <textarea className="txt code" value={edit} onChange={e => setEdit(e.target.value)} placeholder="one name per line" rows={4} />
            <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
              <button className="btn ghost" onClick={() => setEdit(null)}>cancel</button>
              <ConfirmButton className="btn danger" label="save" confirmLabel="confirm: change the reviewers" onConfirm={save} />
            </div>
          </>
        )}
        {msg && <div className={"mx-toast " + (msg.ok ? "ok" : "bad")}>{msg.msg}</div>}
      </div>
    </div>
  );
}

// ============ COUSIN FILE EXPLORER ============
function FileTreeNode({ slug, entry, depth, showHidden, selected, onSelect }) {
  const [open, setOpen] = React.useState(false);
  const [kids, setKids] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const isDir = entry.type === "dir" || (entry.type === "link" && entry.target_type === "dir" && !entry.outside);
  const toggle = async () => {
    if (!isDir) { if (!(entry.type === "link" && entry.outside)) onSelect(entry); return; }
    const next = !open;
    setOpen(next);
    if (next) {
      const d = await apiGet(`/api/cousins/${slug}/files?path=${encodeURIComponent(entry.path)}${showHidden ? "&hidden=1" : ""}`);
      if (d) { setKids(d.entries); setErr(d.truncated ? `showing ${d.entries.length} of ${d.total}` : null); }
      else setErr("unreadable");
    }
  };
  React.useEffect(() => { setKids(null); setOpen(false); }, [showHidden]);
  return (
    <>
      <div className={"ft-row" + (selected === entry.path ? " sel" : "") + (entry.outside ? " outside" : "")}
           style={{ paddingLeft: 8 + depth * 14 }} onClick={toggle} data-path={entry.path}
           title={entry.outside ? "link that leaves the home: not followed" : entry.path}>
        <span className="ft-caret">{isDir ? (open ? "v" : ">") : ""}</span>
        <span className={"ft-name" + (isDir ? " dir" : "")}>{entry.name}{entry.type === "link" ? " @" : ""}</span>
        <span className="ft-size">{isDir ? "" : fmtBytes(entry.size)}</span>
        <span className="ft-time">{fmtWhen(entry.mtime)}</span>
      </div>
      {open && err && <div className="ft-row muted" style={{ paddingLeft: 22 + depth * 14 }}>{err}</div>}
      {open && kids && kids.length === 0 && <div className="ft-row muted" style={{ paddingLeft: 22 + depth * 14 }}>(empty)</div>}
      {open && kids && kids.map(k => (
        <FileTreeNode key={k.path} slug={slug} entry={k} depth={depth + 1} showHidden={showHidden} selected={selected} onSelect={onSelect} />
      ))}
    </>
  );
}

function CousinFiles({ slug }) {
  const [showHidden, setShowHidden] = React.useState(false);
  const [root, setRoot] = React.useState(null);
  const [sel, setSel] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet(`/api/cousins/${slug}/files${showHidden ? "?hidden=1" : ""}`).then(d => { if (!cancelled) setRoot(d); });
    return () => { cancelled = true; };
  }, [slug, showHidden]);
  return (
    <div className="ft-wrap" data-file-explorer={slug}>
      <div className="ft-tree">
        <div className="ft-bar">
          <span className="muted">~/{slug}</span>
          <span style={{ flex: 1 }} />
          <label className="muted"><input type="checkbox" checked={showHidden} onChange={e => setShowHidden(e.target.checked)} /> dotfiles</label>
        </div>
        {!root && <div className="mx-empty">loading...</div>}
        {root && root.entries.map(e => (
          <FileTreeNode key={e.path + showHidden} slug={slug} entry={e} depth={0} showHidden={showHidden} selected={sel && sel.path} onSelect={setSel} />
        ))}
      </div>
      <div className="ft-view">
        {sel ? (
          <FileViewer key={sel.path}
            readUrl={`/api/cousins/${slug}/files/read?path=${encodeURIComponent(sel.path)}`}
            downloadUrl={`/api/cousins/${slug}/files/download?path=${encodeURIComponent(sel.path)}`}
            path={sel.path} />
        ) : <div className="mx-empty">select a file; .secrets/ is never shown</div>}
      </div>
    </div>
  );
}

function CousinFilesModal({ slug, onClose }) {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Portalled to the body: the inspector drawer is its own stacking
  // context, and the explorer wants the whole viewport.
  return ReactDOM.createPortal((
    <div className="modal-bg" onClick={onClose}>
      <div className="modal ft-modal" onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>files · @{slug}</span>
          <span style={{ marginLeft: "auto", color: "var(--fg-3)", fontSize: 10 }}>read-only</span>
          <button className="close" onClick={onClose}>×</button>
        </div>
        <div className="body" style={{ padding: 0, flex: 1, minHeight: 0, display: "flex" }}>
          <CousinFiles slug={slug} />
        </div>
      </div>
    </div>
  ), document.body);
}

Object.assign(window, {
  MemoryExplorer, CousinFiles, CousinFilesModal, FileViewer, MarkdownDoc,
  sanitizeHtml, ConfirmButton, fmtBytes, groupByLevel, entryCite, LEVEL_META,
  SharedReviewersPanel, hitLegsText, reviewBody, claimRetireBody, portraitCommitReady,
});
