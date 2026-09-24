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
function ConfirmButton({ label, confirmLabel, onConfirm, className, title }) {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button className={className || "btn danger"} title={title}
            data-confirm={armed ? "armed" : "idle"}
            onClick={(e) => { e.stopPropagation(); if (armed) { setArmed(false); onConfirm(); } else setArmed(true); }}>
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
    const { r, d } = await apiSend("POST", `/api/memory/${slug}/obsolete`, { topic, why: why.trim() });
    if (!r.ok || !d.ok) { flash({ ok: false, msg: `could not mark "${topic}" obsolete: ${d.error || r.status}` }); return false; }
    flash({ ok: true, msg: `marked "${topic}" obsolete${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}` });
    setTick(t => t + 1);
    refresh();
    return true;
  };
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
                      onRemove={remove} onObsolete={markObsolete} />}
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

function MemoryInsights({ ov, only, onLevel }) {
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

function RawEntries({ slug, tier, reload, onRemove, onObsolete, levels: levelsProp, setLevels: setLevelsProp, onCounts }) {
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
          {e.topic && e.level !== "L5_OBSOLETE" && onObsolete &&
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
});
