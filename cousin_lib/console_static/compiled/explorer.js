function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " kB";
  return n + " B";
}
function fmtWhen(epoch) {
  if (!epoch) return "-";
  return fmtAgo(Date.now() / 1e3 - epoch);
}
function sanitizeHtml(html) {
  try {
    const doc = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
    const root = doc.body.firstChild;
    root.querySelectorAll("script,style,iframe,object,embed,link,meta,base,form").forEach((n) => n.remove());
    root.querySelectorAll("*").forEach((el) => {
      for (const a of [...el.attributes]) {
        const name = a.name.toLowerCase();
        const val = (a.value || "").trim().toLowerCase();
        if (name.startsWith("on")) el.removeAttribute(a.name);
        else if ((name === "href" || name === "src" || name === "xlink:href") && (val.startsWith("javascript:") || val.startsWith("data:text"))) el.removeAttribute(a.name);
      }
      if (el.tagName === "A") {
        el.setAttribute("target", "_blank");
        el.setAttribute("rel", "noopener noreferrer");
      }
    });
    return root.innerHTML;
  } catch (_e) {
    return escapeHtml(html);
  }
}
function MarkdownDoc({ text }) {
  const html = React.useMemo(() => sanitizeHtml(renderMarkdown(text || "")), [text]);
  return /* @__PURE__ */ React.createElement("div", { className: "chat-md mx-md", "data-md-doc": true, dangerouslySetInnerHTML: { __html: html } });
}
function LinesView({ page }) {
  const start = page.start || 1;
  return /* @__PURE__ */ React.createElement("pre", { className: "mx-lines", "data-lines-view": true }, (page.lines || []).map((l, i) => /* @__PURE__ */ React.createElement("div", { key: start + i, className: "mx-line" }, /* @__PURE__ */ React.createElement("span", { className: "mx-ln" }, start + i), /* @__PURE__ */ React.createElement("span", { className: "mx-lt" }, l || " "))));
}
function FileViewer({ readUrl, downloadUrl, path, onDelete, deleteLabel }) {
  const [page, setPage] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [raw, setRaw] = React.useState(false);
  const PAGE = 1e3;
  const load = React.useCallback(async (start) => {
    setErr(null);
    const sep = readUrl.includes("?") ? "&" : "?";
    try {
      const r = await fetch(`${readUrl}${sep}start=${start}&count=${PAGE}`, { cache: "no-store" });
      const d = await r.json();
      if (!r.ok) {
        setErr(d.error || `HTTP ${r.status}`);
        setPage(null);
        return;
      }
      setPage(d);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, [readUrl]);
  React.useEffect(() => {
    setPage(null);
    setRaw(false);
    load(1);
  }, [load]);
  const head = /* @__PURE__ */ React.createElement("div", { className: "mx-filehdr" }, /* @__PURE__ */ React.createElement("span", { className: "mx-path", title: path }, path), page && /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtBytes(page.size), " · ", fmtWhen(page.mtime)), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), page && page.kind === "markdown" && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => setRaw((v) => !v) }, raw ? "rendered" : "raw"), downloadUrl && /* @__PURE__ */ React.createElement("a", { className: "btn ghost", href: downloadUrl, download: true }, "download"), onDelete && /* @__PURE__ */ React.createElement("button", { className: "btn danger", onClick: onDelete }, deleteLabel || "delete"));
  if (err) return /* @__PURE__ */ React.createElement("div", { className: "mx-viewer" }, head, /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, err));
  if (!page) return /* @__PURE__ */ React.createElement("div", { className: "mx-viewer" }, head, /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "loading..."));
  let body;
  if (page.kind === "markdown") {
    body = raw ? /* @__PURE__ */ React.createElement(LinesView, { page: { start: 1, lines: (page.text || "").split("\n") } }) : /* @__PURE__ */ React.createElement(MarkdownDoc, { text: page.text });
  } else if (page.kind === "text") {
    const first = page.start, last = page.start + page.lines.length - 1;
    body = /* @__PURE__ */ React.createElement(React.Fragment, null, (page.start > 1 || page.more) && /* @__PURE__ */ React.createElement("div", { className: "mx-pager" }, "lines ", first, "-", last, " of ", page.total_lines, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: page.start <= 1, onClick: () => load(Math.max(1, page.start - PAGE)) }, "prev"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: !page.more, onClick: () => load(page.start + PAGE) }, "next"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: !page.more, onClick: () => load(Math.max(1, page.total_lines - PAGE + 1)) }, "end")), /* @__PURE__ */ React.createElement(LinesView, { page }));
  } else if (page.kind === "image") {
    body = /* @__PURE__ */ React.createElement("div", { className: "mx-img" }, /* @__PURE__ */ React.createElement("img", { src: downloadUrl, alt: path }));
  } else {
    body = /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "binary file (", page.mime || "unknown type", ", ", fmtBytes(page.size), "): not shown as text. Use download.");
  }
  return /* @__PURE__ */ React.createElement("div", { className: "mx-viewer" }, head, /* @__PURE__ */ React.createElement("div", { className: "mx-viewer-body" }, body));
}
const TRUTH_LEVELS = [
  "L0_OPERATOR",
  "L1_FRAMEWORK",
  "L2_TOOL",
  "L3_COUSIN_CONCLUSION",
  "L4_COUSIN_HYPOTHESIS",
  "L5_OBSOLETE"
];
const LEVEL_META = {
  L0_OPERATOR: { label: "operator", tone: "cyan", note: "what the operator said, with where" },
  L1_FRAMEWORK: { label: "framework", tone: "violet", note: "state changes the framework made" },
  L2_TOOL: { label: "tool", tone: "green", note: "a measurement or a command's output" },
  L3_COUSIN_CONCLUSION: { label: "conclusion", tone: "blue", note: "the cousin's own reasoning" },
  L4_COUSIN_HYPOTHESIS: { label: "hypothesis", tone: "amber", note: "unverified" },
  L5_OBSOLETE: { label: "obsolete", tone: "gray", note: "superseded: kept, struck through" },
  other: { label: "other", tone: "red", note: "a level the framework does not know" }
};
const LEVEL_TONE = Object.fromEntries(Object.entries(LEVEL_META).map(([k, v]) => [k, v.tone]));
const LEVEL_SHORT = (l) => (l || "").replace(/^(L\d)_.*/, "$1") || "?";
const levelMeta = (l) => LEVEL_META[l] || LEVEL_META.other;
function groupByLevel(entries) {
  const order = TRUTH_LEVELS.concat(["other"]);
  const groups = {};
  for (const e of entries || []) {
    const lvl = TRUTH_LEVELS.includes(e.level) ? e.level : "other";
    (groups[lvl] = groups[lvl] || []).push(e);
  }
  return order.filter((l) => groups[l]).map((l) => ({ level: l, entries: groups[l] }));
}
function entryCite(e) {
  const c = e ? e.cite != null ? e.cite : e.extra && e.extra.cite : null;
  return c == null || String(c).trim() === "" ? null : String(c);
}
function fmtStamp(iso) {
  if (!iso) return "undated";
  return String(iso).replace("T", " ").slice(0, 16);
}
function ConfirmButton({ label, confirmLabel, onConfirm, className, title, disabled }) {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4e3);
    return () => clearTimeout(t);
  }, [armed]);
  React.useEffect(() => {
    if (disabled) setArmed(false);
  }, [disabled]);
  return /* @__PURE__ */ React.createElement(
    "button",
    {
      className: className || "btn danger",
      title,
      disabled: !!disabled,
      "data-confirm": armed ? "armed" : "idle",
      onClick: (e) => {
        e.stopPropagation();
        if (disabled) return;
        if (armed) {
          setArmed(false);
          onConfirm();
        } else setArmed(true);
      }
    },
    armed ? confirmLabel || "confirm: move to trash" : label || "delete"
  );
}
function MemoryExplorer({ slug }) {
  const [ov, setOv] = React.useState(null);
  const [layer, setLayer] = React.useState("insights");
  const [toast, setToast] = React.useState(null);
  const [tick, setTick] = React.useState(0);
  const [levels, setLevels] = React.useState([]);
  const [levelCounts, setLevelCounts] = React.useState(null);
  React.useEffect(() => {
    setLevels([]);
    setLevelCounts(null);
  }, [slug]);
  const [badges, setBadges] = React.useState({});
  const [historyTopic, setHistoryTopic] = React.useState("");
  React.useEffect(() => {
    setHistoryTopic("");
  }, [slug]);
  const [writer] = useMemoryJson(`/api/memory/${slug}/writer`, 0);
  const isOperator = !!(writer && writer.can_write_operator);
  React.useEffect(() => {
    let cancelled = false;
    Promise.all([apiGet(`/api/memory/${slug}/tensions`), apiGet(`/api/memory/${slug}/review`)]).then(([t, r]) => {
      if (!cancelled) setBadges({ tensions: t ? t.tensions.length : null, held: r ? r.held.length : null });
    });
    return () => {
      cancelled = true;
    };
  }, [slug, tick]);
  const refresh = React.useCallback(async () => {
    const d = await apiGet(`/api/memory/${slug}/overview`);
    if (d) setOv(d);
  }, [slug]);
  React.useEffect(() => {
    setOv(null);
    setLayer("insights");
    refresh();
  }, [slug, refresh]);
  const flash = (t) => {
    setToast(t);
    setTimeout(() => setToast((x) => x === t ? null : x), 8e3);
  };
  const remove = async (payload, what) => {
    const { r, d } = await apiSend("POST", `/api/memory/${slug}/delete`, payload);
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `could not remove ${what}: ${d.error || r.status}` });
      return false;
    }
    flash({ ok: true, msg: `moved ${what} to trash${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}`, undo: d.trash.id });
    setTick((t) => t + 1);
    refresh();
    return true;
  };
  const markObsolete = async (topic) => {
    const why = window.prompt(`Mark topic "${topic}" obsolete?
It leaves the distilled views; raw keeps its history, and a later entry on the topic revives it.

Why (what superseded it)?`, "");
    if (why === null) return false;
    if (!why.trim()) {
      flash({ ok: false, msg: "not marked: a reason is required" });
      return false;
    }
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/obsolete`, { topic, why: why.trim() });
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `could not mark "${topic}" obsolete: ${d.error || r.status}` });
      return false;
    }
    flash({ ok: true, msg: `marked "${topic}" obsolete${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}` });
    setTick((t) => t + 1);
    refresh();
    return true;
  };
  const retireClaim = async (body) => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/obsolete`, body);
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `claim not retired: ${d.error || r.status}` });
      return false;
    }
    flash({ ok: true, msg: `retired claim ${body.entry} of "${body.topic}"${d.effects && d.effects.distilled ? " · distilled views regenerated" : ""}` });
    setTick((t) => t + 1);
    refresh();
    return true;
  };
  const openHistory = (topic) => {
    setHistoryTopic(topic || "");
    setLayer("op-history");
  };
  const changed = () => {
    setTick((t) => t + 1);
    refresh();
  };
  const restore = async (id) => {
    const { r, d } = await apiSend("POST", `/api/memory/${slug}/restore`, { id });
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `restore failed: ${d.error || r.status}` });
      return;
    }
    flash({ ok: true, msg: `restored ${(d.restored.items || []).length} item(s)` });
    setTick((t) => t + 1);
    refresh();
  };
  if (!ov) return /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "loading @", slug, " memory...");
  const rawLayer = layer === "raw" || layer === "digest" || layer === "archive";
  const counts = rawLayer && levelCounts || ov.insights && ov.insights.levels || {};
  const countTotal = Object.values(counts).reduce((a, b) => a + (Number(b) || 0), 0);
  const pickLevel = (lvl) => {
    if (!rawLayer) setLayer("raw");
    if (lvl === null) {
      setLevels([]);
      return;
    }
    setLevels((ls) => ls.includes(lvl) ? ls.filter((x) => x !== lvl) : [...ls, lvl]);
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
    ["op-moments", "capsules and callbacks", "read-only"]
  ];
  const layers = ov.layers || [];
  const byId = Object.fromEntries(layers.map((l) => [l.id, l]));
  const groups = [
    ["now", ["active", "index"]],
    ["candidates", ["raw", "digest", "archive"]],
    ["durable", ["distilled", "decisions", "memory", "notes", "harness"]],
    ["machinery", ["search", "recall", "trash", "legacy"]]
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
  return /* @__PURE__ */ React.createElement("div", { className: "mx-grid", "data-memory-explorer": slug }, /* @__PURE__ */ React.createElement("div", { className: "mx-side panel" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "@", slug, " memory")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 0, overflowY: "auto", flex: 1 } }, /* @__PURE__ */ React.createElement("div", { className: "mx-group" }, "truth level"), /* @__PURE__ */ React.createElement("div", { className: "mx-levels", "data-level-rail": true }, /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "mx-lvl" + (rawLayer && !levels.length ? " sel" : ""),
      onClick: () => pickLevel(null),
      title: "every level"
    },
    /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-bar tone-all" }),
    /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-n" }, "all levels"),
    /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-c" }, countTotal)
  ), TRUTH_LEVELS.map((l) => /* @__PURE__ */ React.createElement(
    "div",
    {
      key: l,
      "data-level": l,
      className: "mx-lvl lvl-" + LEVEL_SHORT(l) + (levels.includes(l) ? " sel" : "") + (counts[l] ? "" : " dim"),
      onClick: () => pickLevel(l),
      title: `${l}: ${levelMeta(l).note}. Click to filter; click again to clear.`
    },
    /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-bar tone-" + levelMeta(l).tone }),
    /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-n" }, levelMeta(l).label),
    /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-c" }, counts[l] || 0)
  )), counts.other ? /* @__PURE__ */ React.createElement("div", { className: "mx-lvl dim", title: LEVEL_META.other.note }, /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-bar tone-red" }), /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-n" }, "other"), /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-c" }, counts.other)) : null), /* @__PURE__ */ React.createElement("div", { className: "mx-group" }, "layers"), /* @__PURE__ */ React.createElement("div", { className: "mx-layer" + (layer === "insights" ? " sel" : ""), onClick: () => setLayer("insights"), "data-layer": "insights" }, /* @__PURE__ */ React.createElement("div", { className: "mx-layer-t" }, "insights"), /* @__PURE__ */ React.createElement("div", { className: "mx-layer-s" }, "levels, recall, hygiene")), /* @__PURE__ */ React.createElement("div", { className: "mx-group" }, "operator"), actions.map(([id, title, sub]) => /* @__PURE__ */ React.createElement("div", { key: id, "data-layer": id, className: "mx-layer" + (layer === id ? " sel" : ""), onClick: () => setLayer(id) }, /* @__PURE__ */ React.createElement("div", { className: "mx-layer-t" }, title), /* @__PURE__ */ React.createElement("div", { className: "mx-layer-s" }, sub))), groups.map(([g, ids]) => /* @__PURE__ */ React.createElement("div", { key: g }, /* @__PURE__ */ React.createElement("div", { className: "mx-group" }, g), ids.map((id) => byId[id] && /* @__PURE__ */ React.createElement(
    "div",
    {
      key: id,
      "data-layer": id,
      className: "mx-layer" + (layer === id ? " sel" : "") + (byId[id].count ? "" : " dim"),
      onClick: () => setLayer(id)
    },
    /* @__PURE__ */ React.createElement("div", { className: "mx-layer-t" }, byId[id].title),
    /* @__PURE__ */ React.createElement("div", { className: "mx-layer-s" }, layerLabel(byId[id]), " · ", fmtWhen(byId[id].updated))
  )))))), /* @__PURE__ */ React.createElement("div", { className: "mx-main" }, toast && /* @__PURE__ */ React.createElement("div", { className: "mx-toast " + (toast.ok ? "ok" : "bad") }, toast.msg, toast.undo && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => {
    restore(toast.undo);
    setToast(null);
  } }, "undo")), layer === "insights" && /* @__PURE__ */ React.createElement(MemoryInsights, { ov, slug, onPick: setLayer, onLevel: pickLevel }), rawLayer && /* @__PURE__ */ React.createElement(
    RawEntries,
    {
      key: layer,
      slug,
      tier: layer === "raw" ? "daily" : layer,
      reload: tick,
      levels,
      setLevels,
      onCounts: setLevelCounts,
      onRemove: remove,
      onObsolete: markObsolete,
      onHistory: openHistory,
      operator: isOperator
    }
  ), layer === "op-search" && /* @__PURE__ */ React.createElement(MemorySearch, { slug, onHistory: openHistory }), layer === "op-write" && /* @__PURE__ */ React.createElement(MemoryWrite, { slug, flash, onDone: changed }), layer === "op-tensions" && /* @__PURE__ */ React.createElement(TensionsList, { slug, reload: tick, onRetire: retireClaim, onHistory: openHistory, operator: isOperator }), layer === "op-review" && /* @__PURE__ */ React.createElement(ReviewQueue, { slug, reload: tick, flash, onChanged: changed }), layer === "op-history" && /* @__PURE__ */ React.createElement(TopicHistory, { slug, topic: historyTopic, reload: tick, onRetire: retireClaim, operator: isOperator }), layer === "op-maintain" && /* @__PURE__ */ React.createElement(MemoryMaintenance, { slug, ov, flash, onChanged: changed }), layer === "op-dreams" && /* @__PURE__ */ React.createElement(DreamsView, { slug, reload: tick, flash, onChanged: changed, operator: isOperator }), layer === "op-portrait" && /* @__PURE__ */ React.createElement(SelfPortrait, { slug, flash, onChanged: changed }), layer === "op-moments" && /* @__PURE__ */ React.createElement(MomentsList, { slug, reload: tick }), layer === "decisions" && /* @__PURE__ */ React.createElement(DecisionList, { slug, reload: tick, onRemove: remove }), ["active", "index", "distilled", "memory", "notes", "harness", "legacy"].includes(layer) && /* @__PURE__ */ React.createElement(LayerFiles, { key: layer, slug, layer, info: byId[layer], reload: tick, onRemove: remove }), layer === "trash" && /* @__PURE__ */ React.createElement(TrashList, { slug, reload: tick, onRestore: restore }), layer === "search" && /* @__PURE__ */ React.createElement(SearchInfo, { l: byId.search }), layer === "recall" && /* @__PURE__ */ React.createElement(MemoryInsights, { ov, slug, onPick: setLayer, only: "recall" })));
}
function BarRow({ label, value, max, tone, onClick }) {
  const pct = max ? Math.round(value / max * 100) : 0;
  return /* @__PURE__ */ React.createElement("div", { className: "mx-barrow", onClick, style: { cursor: onClick ? "pointer" : "default" } }, /* @__PURE__ */ React.createElement("span", { className: "mx-barlabel" }, label), /* @__PURE__ */ React.createElement("span", { className: "mx-bar" }, /* @__PURE__ */ React.createElement("span", { className: "tone-" + (tone || "gray"), style: { width: pct + "%" } })), /* @__PURE__ */ React.createElement("span", { className: "mx-barval" }, value));
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
  const recall = /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "most recalled"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, ins.recall_events || 0, " recall events · last ", fmtStamp(ins.last_recall))), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, (ins.most_recalled || []).length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no recall recorded yet"), (ins.most_recalled || []).map((r) => /* @__PURE__ */ React.createElement(BarRow, { key: r.path, label: r.path, value: r.count, max: (ins.most_recalled[0] || {}).count, tone: "cyan" })), (ins.cold_files || []).length > 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-sub" }, "never recalled: ", ins.cold_files.join(", "))));
  if (only === "recall") return /* @__PURE__ */ React.createElement("div", { className: "mx-cards" }, recall);
  return /* @__PURE__ */ React.createElement("div", { className: "mx-cards", "data-insights": true }, /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "entries per truth level"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, ins.topics || 0, " topics · ", ins.multi_entry_topics || 0, " with history")), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, Object.entries(levels).map(([lvl, n]) => /* @__PURE__ */ React.createElement(
    BarRow,
    {
      key: lvl,
      label: `${LEVEL_SHORT(lvl)} ${levelMeta(lvl).label}`,
      value: n,
      max: maxLevel,
      tone: levelMeta(lvl).tone,
      onClick: onLevel && lvl !== "other" ? () => onLevel(lvl) : void 0
    }
  )))), /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "hygiene")), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, flags.length === 0 ? /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "nothing to flag") : /* @__PURE__ */ React.createElement("ul", { className: "mx-flags" }, flags.map((f) => /* @__PURE__ */ React.createElement("li", { key: f }, f))), onPick && (ins.distilled_behind_raw || ins.pending_fold_files) ? /* @__PURE__ */ React.createElement("button", { className: "btn ghost", "data-open-maintenance": true, onClick: () => onPick("op-maintain") }, "open maintenance") : null)), recall, /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "sources and busy topics")), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, Object.entries(ins.sources || {}).map(([s, n]) => /* @__PURE__ */ React.createElement(BarRow, { key: s, label: s, value: n, max: Math.max(...Object.values(ins.sources)), tone: "violet" })), (ins.top_topics || []).length > 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-sub" }, "topics with the most entries:"), (ins.top_topics || []).map((t) => /* @__PURE__ */ React.createElement(BarRow, { key: t.topic, label: t.topic, value: t.entries, max: ins.top_topics[0].entries, tone: "amber" })))), months.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "daily raw entries per month")), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, months.map(([m, n]) => /* @__PURE__ */ React.createElement(BarRow, { key: m, label: m, value: n, max: maxMonth, tone: "green" })))));
}
function SearchInfo({ l }) {
  if (!l) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "mx-cards" }, /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "keyword index")), /* @__PURE__ */ React.createElement("div", { className: "panel-body mx-kv" }, l.fts && l.fts.exists ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", null, "files indexed · ", /* @__PURE__ */ React.createElement("b", null, l.fts.files)), /* @__PURE__ */ React.createElement("div", null, "built · ", /* @__PURE__ */ React.createElement("b", null, fmtWhen(l.fts.built_at))), /* @__PURE__ */ React.createElement("div", null, "state · ", /* @__PURE__ */ React.createElement("b", null, l.fts.stale === null || l.fts.stale === void 0 ? "unknown" : l.fts.stale ? "stale: rebuilds on the next search" : "current"))) : /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no keyword index yet; the first search builds it"))), /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "semantic index")), /* @__PURE__ */ React.createElement("div", { className: "panel-body mx-kv" }, l.embeddings && l.embeddings.exists ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", null, "size · ", /* @__PURE__ */ React.createElement("b", null, fmtBytes(l.embeddings.size))), l.embeddings.chunks !== void 0 && /* @__PURE__ */ React.createElement("div", null, "chunks · ", /* @__PURE__ */ React.createElement("b", null, l.embeddings.chunks))) : /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no embeddings (semantic search not configured, or never run)"))));
}
function RawEntries({ slug, tier, reload, onRemove, onObsolete, onHistory, operator, levels: levelsProp, setLevels: setLevelsProp, onCounts }) {
  const [ownLevels, setOwnLevels] = React.useState([]);
  const levels = levelsProp || ownLevels;
  const setLevels = setLevelsProp || setOwnLevels;
  const [topic, setTopic] = React.useState("");
  const [q, setQ] = React.useState("");
  const [since, setSince] = React.useState("");
  const [until, setUntil] = React.useState("");
  const [data, setData] = React.useState(null);
  const [limit, setLimit] = React.useState(100);
  const [grouping, setGrouping] = React.useState("level");
  React.useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      const p = new URLSearchParams({ tier, limit: String(limit) });
      if (topic.trim()) p.set("topic", topic.trim());
      if (q.trim()) p.set("q", q.trim());
      if (since) p.set("since", since);
      if (until) p.set("until", until);
      const all = new URLSearchParams(p);
      all.set("limit", "1");
      if (levels.length) p.set("level", levels.join(","));
      const [d, whole] = await Promise.all([
        apiGet(`/api/memory/${slug}/raw?` + p.toString()),
        levels.length ? apiGet(`/api/memory/${slug}/raw?` + all.toString()) : Promise.resolve(null)
      ]);
      if (cancelled) return;
      const got = d || { entries: [], total: 0, facets: {} };
      setData(got);
      if (onCounts) onCounts(((whole || got).facets || {}).levels || {});
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [slug, tier, levels.join(","), topic, q, since, until, limit, reload]);
  const facets = data && data.facets && data.facets.levels || {};
  const renderEntry = (e) => {
    const meta = levelMeta(e.level);
    const cite = entryCite(e);
    const extra = Object.entries(e.extra || {}).filter(([k]) => !(k === "cite" && e.level === "L0_OPERATOR"));
    return /* @__PURE__ */ React.createElement(
      "div",
      {
        key: e.ref ? e.ref.path + ":" + e.ref.line_no : e.file + e.timestamp + e.topic,
        className: "mx-entry lvl-" + LEVEL_SHORT(e.level),
        "data-entry-level": e.level
      },
      /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement("span", { className: "pill " + meta.tone, title: e.truth_level || "(no level stored: default)" }, LEVEL_SHORT(e.level), " ", meta.label), /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, e.topic || /* @__PURE__ */ React.createElement("i", null, "no topic")), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(e.timestamp))),
      /* @__PURE__ */ React.createElement("div", { className: "mx-content" }, e.content),
      e.level === "L0_OPERATOR" && /* @__PURE__ */ React.createElement("div", { className: "mx-cite" + (cite ? "" : " missing"), "data-cite": true }, cite ? /* @__PURE__ */ React.createElement(React.Fragment, null, "cited · ", /* @__PURE__ */ React.createElement("b", null, cite)) : "no citation stored"),
      /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, e.source && /* @__PURE__ */ React.createElement("span", null, "source · ", /* @__PURE__ */ React.createElement("b", null, e.source)), e.tier === "digest" && /* @__PURE__ */ React.createElement("span", null, "folded · ", /* @__PURE__ */ React.createElement("b", null, e.entries), " entries ", e.first_at, " to ", e.last_at), extra.map(([k, v]) => /* @__PURE__ */ React.createElement("span", { key: k }, k, " · ", /* @__PURE__ */ React.createElement("b", null, typeof v === "object" ? JSON.stringify(v) : String(v)))), /* @__PURE__ */ React.createElement("span", { className: "muted" }, e.file, e.ref ? ":" + e.ref.line_no : ""), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), e.topic && onHistory && /* @__PURE__ */ React.createElement(
        "button",
        {
          className: "btn ghost",
          "data-history": e.topic,
          title: "the topic's claims with their valid time",
          onClick: (ev) => {
            ev.stopPropagation();
            onHistory(e.topic);
          }
        },
        "history"
      ), e.topic && e.level !== "L5_OBSOLETE" && onObsolete && (e.level !== "L0_OPERATOR" || operator) && /* @__PURE__ */ React.createElement(
        "button",
        {
          className: "btn ghost",
          "data-mark-obsolete": e.topic,
          title: "mark this topic superseded (L5): out of the distilled views, history kept in raw",
          onClick: (ev) => {
            ev.stopPropagation();
            onObsolete(e.topic);
          }
        },
        "mark obsolete"
      ), e.ref && /* @__PURE__ */ React.createElement(
        ConfirmButton,
        {
          className: "btn ghost danger-text",
          label: "remove",
          onConfirm: () => onRemove({ kind: "entry", ...e.ref }, `entry "${e.topic}"`)
        }
      ))
    );
  };
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "mx-filters", "data-raw-filters": true }, /* @__PURE__ */ React.createElement("div", { className: "mx-inputs" }, /* @__PURE__ */ React.createElement("input", { placeholder: "topic", value: topic, onChange: (e) => setTopic(e.target.value) }), /* @__PURE__ */ React.createElement("input", { placeholder: "text", value: q, onChange: (e) => setQ(e.target.value) }), /* @__PURE__ */ React.createElement("label", null, "from ", /* @__PURE__ */ React.createElement("input", { type: "date", value: since, onChange: (e) => setSince(e.target.value) })), /* @__PURE__ */ React.createElement("label", null, "to ", /* @__PURE__ */ React.createElement("input", { type: "date", value: until, onChange: (e) => setUntil(e.target.value) })), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("div", { className: "radio-row", "data-grouping": grouping }, /* @__PURE__ */ React.createElement("button", { className: grouping === "level" ? "sel" : "", onClick: () => setGrouping("level") }, "by level"), /* @__PURE__ */ React.createElement("button", { className: grouping === "time" ? "sel" : "", onClick: () => setGrouping("time") }, "newest first"))), /* @__PURE__ */ React.createElement("div", { className: "muted mx-count" }, data ? `${data.total} ${tier} entries match` : "loading...", levels.length > 0 && /* @__PURE__ */ React.createElement(React.Fragment, null, " · levels: ", levels.map((l) => levelMeta(l).label).join(", "), " ", /* @__PURE__ */ React.createElement("button", { className: "btn ghost mx-clear", onClick: () => setLevels([]) }, "clear")), data && Object.keys(facets).length > 0 && " · " + Object.entries(facets).map(([k, v]) => `${LEVEL_SHORT(k)} ${v}`).join(" · "), tier === "archive" && " · the archive is the forensic tier: read-only")), /* @__PURE__ */ React.createElement("div", { className: "mx-list", "data-raw-list": true }, data && data.entries.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no entries"), data && grouping === "level" && groupByLevel(data.entries).map((g) => /* @__PURE__ */ React.createElement("section", { key: g.level, className: "mx-lgroup lvl-" + LEVEL_SHORT(g.level), "data-level-group": g.level }, /* @__PURE__ */ React.createElement("div", { className: "mx-lgroup-h" }, /* @__PURE__ */ React.createElement("span", { className: "mx-lvl-bar tone-" + levelMeta(g.level).tone }), /* @__PURE__ */ React.createElement("span", { className: "mx-lgroup-t" }, levelMeta(g.level).label), /* @__PURE__ */ React.createElement("span", { className: "mx-lgroup-c" }, g.entries.length, facets[g.level] > g.entries.length ? ` of ${facets[g.level]}` : ""), /* @__PURE__ */ React.createElement("span", { className: "mx-lgroup-note" }, levelMeta(g.level).note)), g.entries.map(renderEntry))), data && grouping === "time" && data.entries.map(renderEntry), data && data.total > data.entries.length && /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => setLimit((l) => Math.min(1e3, l + 200)) }, "show more (", data.total - data.entries.length, " left)")));
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
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [slug, q, limit, reload]);
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "mx-filters" }, /* @__PURE__ */ React.createElement("div", { className: "mx-inputs" }, /* @__PURE__ */ React.createElement("input", { placeholder: "search topic, decision, reasoning", value: q, onChange: (e) => setQ(e.target.value), style: { flex: 1 } }), /* @__PURE__ */ React.createElement("label", { title: "decide also wrote each decision into memory/raw; remove that copy too" }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: withMirrors, onChange: (e) => setWithMirrors(e.target.checked) }), " remove the raw copy too")), /* @__PURE__ */ React.createElement("div", { className: "muted mx-count" }, data ? `${data.total} decisions` : "loading...")), /* @__PURE__ */ React.createElement("div", { className: "mx-list" }, data && data.entries.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no decisions"), data && data.entries.map((d) => /* @__PURE__ */ React.createElement("div", { key: d.ref ? d.ref.line_no + d.ref.sha : d.file + d.timestamp, className: "mx-entry mx-decision" }, /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, d.topic), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(d.timestamp))), /* @__PURE__ */ React.createElement("div", { className: "mx-content" }, /* @__PURE__ */ React.createElement("b", null, "decided:"), " ", d.decision), /* @__PURE__ */ React.createElement("div", { className: "mx-content mx-why" }, /* @__PURE__ */ React.createElement("b", null, "why:"), " ", d.reasoning), /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, d.mirrors.length ? `${d.mirrors.length} raw copy` : "no raw copy"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), d.ref && /* @__PURE__ */ React.createElement(
    ConfirmButton,
    {
      className: "btn ghost danger-text",
      label: "remove",
      onConfirm: () => onRemove({ kind: "decision", ...d.ref, mirrors: withMirrors }, `decision "${d.topic}"`)
    }
  )))), data && data.total > data.entries.length && /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => setLimit((l) => Math.min(1e3, l + 200)) }, "show more")));
}
function LayerFiles({ slug, layer, info, reload, onRemove }) {
  const [files, setFiles] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet(`/api/memory/${slug}/files?layer=${layer}`).then((d) => {
      if (cancelled) return;
      const list = d && d.files || [];
      setFiles(list);
      setOpen((cur2) => cur2 && list.some((f) => f.path === cur2) ? cur2 : list.length ? list[0].path : null);
    });
    return () => {
      cancelled = true;
    };
  }, [slug, layer, reload]);
  const extra = layer === "harness" ? "&layer=harness" : "";
  const cur = files && files.find((f) => f.path === open);
  const hint = {
    distilled: "generated from raw by the distiller: remove the raw entries, not these files",
    active: "session state the boot packet reads first; edited by the cousin, read-only here",
    index: "the pointer index; read-only here",
    harness: info && info.directory ? `the harness's own auto-memory: ${info.directory} (read-only here)` : "no harness auto-memory directory configured",
    legacy: "the pre-migration archive: removal asks twice and still goes to trash"
  }[layer];
  return /* @__PURE__ */ React.createElement("div", { className: "mx-files" }, /* @__PURE__ */ React.createElement("div", { className: "mx-filelist panel" }, hint && /* @__PURE__ */ React.createElement("div", { className: "mx-hint" }, hint), files && files.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no files"), (files || []).map((f) => /* @__PURE__ */ React.createElement("div", { key: f.path, "data-file": f.path, className: "mx-file" + (open === f.path ? " sel" : ""), onClick: () => setOpen(f.path) }, /* @__PURE__ */ React.createElement("div", { className: "mx-file-n" }, f.path, f.stub ? /* @__PURE__ */ React.createElement("span", { className: "pill gray", style: { marginLeft: 6 } }, "empty") : null), /* @__PURE__ */ React.createElement("div", { className: "mx-file-s" }, fmtBytes(f.size), " · ", fmtWhen(f.mtime), f.recalls ? ` · recalled ${f.recalls}x` : "")))), /* @__PURE__ */ React.createElement("div", { className: "mx-filepane" }, cur ? /* @__PURE__ */ React.createElement(
    FileViewer,
    {
      key: cur.path,
      readUrl: `/api/memory/${slug}/file?path=${encodeURIComponent(cur.path)}${extra}`,
      downloadUrl: layer === "harness" ? null : `/api/cousins/${slug}/files/download?path=${encodeURIComponent(cur.path)}`,
      path: cur.path,
      deleteLabel: cur.legacy ? "remove legacy file" : "remove",
      onDelete: cur.deletable ? () => {
        const msg = cur.legacy ? `${cur.path} is part of the legacy archive. Move it to the trash anyway?` : `Move ${cur.path} to the trash? It can be restored from the trash layer.`;
        if (!window.confirm(msg)) return;
        onRemove({ kind: "file", path: cur.path, legacy: !!cur.legacy }, cur.path).then((ok) => {
          if (ok) setOpen(null);
        });
      } : null
    }
  ) : /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "select a file")));
}
function TrashList({ slug, reload, onRestore }) {
  const [batches, setBatches] = React.useState(null);
  React.useEffect(() => {
    apiGet(`/api/memory/${slug}/trash`).then((d) => setBatches(d && d.batches || []));
  }, [slug, reload]);
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "mx-hint" }, "removed memories are kept here under memory/.trash/ with their original path; restore puts them back (also: cousin-memory trash restore ID)"), /* @__PURE__ */ React.createElement("div", { className: "mx-list", "data-trash-list": true }, batches && batches.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "trash is empty"), (batches || []).map((b) => /* @__PURE__ */ React.createElement("div", { key: b.id, className: "mx-entry", "data-trash-id": b.id }, /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, b.id), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "by ", b.by || "-", " · ", fmtStamp(b.deleted_at)), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onRestore(b.id) }, "restore")), (b.items || []).map((it, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "mx-content" }, it.kind === "file" ? /* @__PURE__ */ React.createElement(React.Fragment, null, "file ", /* @__PURE__ */ React.createElement("b", null, it.path), " (", fmtBytes(it.size), ")") : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "muted" }, it.path, ":", it.line_no), " ", /* @__PURE__ */ React.createElement(TrashLine, { line: it.line }))))))));
}
function TrashLine({ line }) {
  let e = null;
  try {
    e = JSON.parse(line);
  } catch (_e) {
  }
  if (!e || typeof e !== "object") return /* @__PURE__ */ React.createElement("code", null, line);
  return /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("b", null, e.topic || "-"), ": ", e.content || e.decision || "");
}
const SEARCH_LEG_LABEL = { keyword: "keyword", semantic: "meaning" };
function hitLegsText(legs) {
  const words = (legs || []).map((l) => SEARCH_LEG_LABEL[l] || l);
  return words.length ? words.join(" + ") : "fused";
}
function reviewBody(marks, why) {
  const verdicts = {};
  for (const [id, v] of Object.entries(marks || {})) {
    if (v === "keep" || v === "drop") verdicts[id] = v;
  }
  return { verdicts, why: String(why || "").trim() };
}
function claimRetireBody(topic, id, why) {
  const w = String(why || "").trim();
  if (!topic || !id || !w) return null;
  return { topic, why: w, entry: id };
}
function portraitCommitReady(typed, slug, sha, dirty) {
  return !!slug && typed === slug && !!sha && !dirty;
}
async function safeSend(method, path, body) {
  try {
    return await apiSend(method, path, body);
  } catch (e) {
    return { r: { ok: false, status: 0 }, d: { ok: false, error: `network: ${e.message || e}` } };
  }
}
function useMemoryJson(url, reload) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!url) {
      setData(null);
      setErr(null);
      return;
    }
    const { r, d } = await safeSend("GET", url);
    if (!r.ok) {
      setErr(d.error || `HTTP ${r.status}`);
      return;
    }
    setErr(null);
    setData(d);
  }, [url]);
  React.useEffect(() => {
    load();
  }, [load, reload]);
  return [data, load, err];
}
function Pending({ data, err }) {
  if (err) return /* @__PURE__ */ React.createElement("div", { className: "mx-toast bad" }, err);
  if (!data) return /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "loading...");
  return null;
}
function LevelPill({ level }) {
  const meta = levelMeta(level);
  return /* @__PURE__ */ React.createElement("span", { className: "pill " + meta.tone, title: level }, LEVEL_SHORT(level), " ", meta.label);
}
function ClaimRetire({ topic, id, onRetire }) {
  const [open, setOpen] = React.useState(false);
  const [why, setWhy] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  if (!open) {
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        "data-retire-claim": id,
        title: "an entry-level obsolete mark: this claim leaves the views, the topic stays, raw keeps it",
        onClick: (e) => {
          e.stopPropagation();
          setOpen(true);
        }
      },
      "retire this claim"
    );
  }
  const body = claimRetireBody(topic, id, why);
  const go = async () => {
    if (!body || busy) return;
    setBusy(true);
    const ok = await onRetire(body);
    setBusy(false);
    if (ok) {
      setOpen(false);
      setWhy("");
    }
  };
  return /* @__PURE__ */ React.createElement("span", { className: "mx-inputs", style: { flex: "1 1 260px" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      placeholder: "why (what superseded it)",
      value: why,
      autoFocus: true,
      style: { flex: 1 },
      onChange: (e) => setWhy(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") go();
        if (e.key === "Escape") setOpen(false);
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn danger", disabled: !body || busy, onClick: go }, "retire"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => {
    setOpen(false);
    setWhy("");
  } }, "cancel"));
}
function claimEnd(validTo) {
  const d = new Date(validTo);
  if (isNaN(d)) return String(validTo || "");
  if (d.getUTCHours() === 0 && d.getUTCMinutes() === 0 && d.getUTCSeconds() === 0) {
    return "through " + new Date(d.getTime() - 1e3).toISOString().slice(0, 10);
  }
  return "until " + d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
function WhyNode({ n, kids }) {
  if (n.missing) return /* @__PURE__ */ React.createElement("li", { className: "muted", "data-why-node": n.id }, n.id, " · ", /^(job|artifact):/.test(n.id) ? "not found" : "not in raw memory");
  if (n.cycle) return /* @__PURE__ */ React.createElement("li", { className: "muted", "data-why-node": n.id }, n.id, " · already on this chain (a cycle)");
  const next = n[kids] || [];
  return /* @__PURE__ */ React.createElement("li", { "data-why-node": n.id }, /* @__PURE__ */ React.createElement(LevelPill, { level: normLevel(n.truth_level) }), " ", /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, n.topic), " ", /* @__PURE__ */ React.createElement("span", { className: "muted" }, n.id), " · ", String(n.content || "").slice(0, 240), n.more && /* @__PURE__ */ React.createElement("span", { className: "muted" }, " (and further)"), next.length > 0 && /* @__PURE__ */ React.createElement("ul", { className: "mx-why" }, next.map((k, i) => /* @__PURE__ */ React.createElement(WhyNode, { key: k.id + i, n: k, kids }))));
}
function WhyTree({ slug, id }) {
  const [data, , err] = useMemoryJson(`/api/memory/${slug}/why?id=${encodeURIComponent(id)}`, 0);
  if (!data) return /* @__PURE__ */ React.createElement(Pending, { data, err });
  return /* @__PURE__ */ React.createElement("div", { className: "mx-why-tree", "data-why": id }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "built from"), data.derived_from.length ? /* @__PURE__ */ React.createElement("ul", { className: "mx-why" }, data.derived_from.map((n, i) => /* @__PURE__ */ React.createElement(WhyNode, { key: n.id + i, n, kids: "built_from" }))) : /* @__PURE__ */ React.createElement("div", { className: "muted" }, "nothing recorded"), /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "built on by"), data.used_by.length ? /* @__PURE__ */ React.createElement("ul", { className: "mx-why" }, data.used_by.map((n, i) => /* @__PURE__ */ React.createElement(WhyNode, { key: n.id + i, n, kids: "built_on_by" }))) : /* @__PURE__ */ React.createElement("div", { className: "muted" }, "nothing"));
}
function ClaimRow({ c, topic, onRetire, showTopic, operator, slug }) {
  const [whyOpen, setWhyOpen] = React.useState(false);
  const live = !c.valid_to || !c.retired_by && new Date(c.valid_to) > /* @__PURE__ */ new Date();
  const theirs = normLevel(c.truth_level) === "L0_OPERATOR" && !operator;
  return /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "mx-entry lvl-" + LEVEL_SHORT(normLevel(c.truth_level)),
      "data-claim": c.id,
      "data-entry-level": live ? normLevel(c.truth_level) : "L5_OBSOLETE"
    },
    /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement(LevelPill, { level: normLevel(c.truth_level) }), showTopic && /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, c.topic), /* @__PURE__ */ React.createElement("span", { className: "muted" }, c.id), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(c.valid_from))),
    /* @__PURE__ */ React.createElement("div", { className: "mx-content" }, c.content),
    /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, c.source && /* @__PURE__ */ React.createElement("span", null, "source · ", /* @__PURE__ */ React.createElement("b", null, c.source)), c.cite && /* @__PURE__ */ React.createElement("span", null, "cite · ", /* @__PURE__ */ React.createElement("b", null, c.cite)), c.scope && /* @__PURE__ */ React.createElement("span", { "data-claim-scope": true }, "scope · ", /* @__PURE__ */ React.createElement("b", null, c.scope)), /* @__PURE__ */ React.createElement("span", null, live ? c.valid_to ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("b", null, "live"), " ", claimEnd(c.valid_to)) : /* @__PURE__ */ React.createElement("b", null, "live") : c.retired_by ? /* @__PURE__ */ React.createElement(React.Fragment, null, "valid to ", /* @__PURE__ */ React.createElement("b", null, fmtStamp(c.valid_to)), ` (mark ${c.retired_by})`) : /* @__PURE__ */ React.createElement(React.Fragment, null, "expired (", claimEnd(c.valid_to).replace(/^through /, "after ").replace(/^until /, ""), ")")), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), slug && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", "data-why-toggle": true, onClick: () => setWhyOpen(!whyOpen) }, whyOpen ? "hide why" : "why"), live && onRetire && !theirs && /* @__PURE__ */ React.createElement(ClaimRetire, { topic: topic || c.topic, id: c.id, onRetire }), live && onRetire && theirs && /* @__PURE__ */ React.createElement("span", { className: "muted", "data-operator-only": true }, "the operator's to retire")),
    whyOpen && slug && /* @__PURE__ */ React.createElement(WhyTree, { slug, id: c.id })
  );
}
function normLevel(l) {
  if (!l) return "L3_COUSIN_CONCLUSION";
  const up = String(l).toUpperCase();
  if (TRUTH_LEVELS.includes(up)) return up;
  const short = {
    "OPERATOR": "L0_OPERATOR",
    "OPERATOR-STATED": "L0_OPERATOR",
    "FRAMEWORK": "L1_FRAMEWORK",
    "TOOL": "L2_TOOL",
    "CONCLUSION": "L3_COUSIN_CONCLUSION",
    "HYPOTHESIS": "L4_COUSIN_HYPOTHESIS",
    "OBSOLETE": "L5_OBSOLETE"
  };
  return short[up] || "other";
}
function TensionsList({ slug, reload, onRetire, onHistory, operator }) {
  const [data, , err] = useMemoryJson(`/api/memory/${slug}/tensions`, reload);
  const list = data && data.tensions || null;
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap", "data-tensions": true }, /* @__PURE__ */ React.createElement("div", { className: "mx-hint" }, "topics whose live claims disagree. Nothing judges which is right: settle one by retiring the claim that no longer holds (an entry-level obsolete mark; raw keeps it)."), /* @__PURE__ */ React.createElement("div", { className: "mx-list" }, /* @__PURE__ */ React.createElement(Pending, { data: list, err }), list && list.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no tensions"), (list || []).map((t) => /* @__PURE__ */ React.createElement("section", { key: t.topic, className: "mx-lgroup", "data-tension": t.topic }, /* @__PURE__ */ React.createElement("div", { className: "mx-lgroup-h" }, /* @__PURE__ */ React.createElement("span", { className: "mx-lgroup-t" }, t.topic), /* @__PURE__ */ React.createElement("span", { className: "mx-lgroup-c" }, t.claims.length, " live claims"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => onHistory(t.topic) }, "history")), t.claims.map((c) => /* @__PURE__ */ React.createElement(ClaimRow, { key: c.id, c, topic: t.topic, onRetire, operator, slug }))))));
}
function TopicHistory({ slug, topic: initial, reload, onRetire, operator }) {
  const [topic, setTopic] = React.useState(initial || "");
  const [asked, setAsked] = React.useState(initial || "");
  React.useEffect(() => {
    setTopic(initial || "");
    setAsked(initial || "");
  }, [initial]);
  const [data, , err] = useMemoryJson(asked ? `/api/memory/${slug}/history?topic=${encodeURIComponent(asked)}` : null, reload);
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap", "data-topic-history": true }, /* @__PURE__ */ React.createElement("div", { className: "mx-filters" }, /* @__PURE__ */ React.createElement("div", { className: "mx-inputs" }, /* @__PURE__ */ React.createElement(
    "input",
    {
      placeholder: "topic (exact)",
      value: topic,
      style: { flex: 1 },
      onChange: (e) => setTopic(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") setAsked(topic.trim());
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: !topic.trim(), onClick: () => setAsked(topic.trim()) }, "show history")), /* @__PURE__ */ React.createElement("div", { className: "muted mx-count" }, "a topic's claims, oldest first, each with its id and valid time (cousin-memory history)")), /* @__PURE__ */ React.createElement("div", { className: "mx-list" }, !asked && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "type a topic"), asked && /* @__PURE__ */ React.createElement(Pending, { data, err }), data && data.claims.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, 'no claims for "', asked, '"'), data && data.claims.map((c) => /* @__PURE__ */ React.createElement(ClaimRow, { key: c.id, c, topic: asked, onRetire, operator, slug }))));
}
function MemorySearch({ slug, onHistory }) {
  const [q, setQ] = React.useState("");
  const [collection, setCollection] = React.useState("");
  const [top, setTop] = React.useState(10);
  const [res, setRes] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [open, setOpen] = React.useState(null);
  React.useEffect(() => {
    setRes(null);
    setOpen(null);
  }, [slug]);
  const run = async () => {
    if (!q.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setOpen(null);
    const p = new URLSearchParams({ q: q.trim(), top: String(top) });
    if (collection) p.set("collection", collection);
    const { r, d } = await safeSend("GET", `/api/memory/${slug}/search?` + p.toString());
    if (!r.ok) {
      setErr(d.error || `HTTP ${r.status}`);
      setRes(null);
    } else setRes(d);
    setBusy(false);
  };
  const semantic = res && {
    on: "on: ranked by meaning too",
    off: "off: config/embedding.toml is not set, keyword only",
    broken: "promised but unusable: keyword only"
  }[res.semantic];
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap", "data-memory-search": true }, /* @__PURE__ */ React.createElement("div", { className: "mx-filters" }, /* @__PURE__ */ React.createElement("div", { className: "mx-inputs" }, /* @__PURE__ */ React.createElement(
    "input",
    {
      placeholder: "search memory, notes, raw entries",
      value: q,
      autoFocus: true,
      style: { flex: 1 },
      onChange: (e) => setQ(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") run();
      }
    }
  ), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: collection, onChange: (e) => setCollection(e.target.value) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "everything"), /* @__PURE__ */ React.createElement("option", { value: "memory" }, "memory files"), /* @__PURE__ */ React.createElement("option", { value: "notes" }, "notes"), /* @__PURE__ */ React.createElement("option", { value: "raw" }, "raw entries"), /* @__PURE__ */ React.createElement("option", { value: "harness" }, "harness auto-memory")), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: top, onChange: (e) => setTop(Number(e.target.value)) }, [5, 10, 20, 50].map((n) => /* @__PURE__ */ React.createElement("option", { key: n, value: n }, "top ", n))), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !q.trim() || busy, onClick: run }, busy ? "searching..." : "search")), /* @__PURE__ */ React.createElement("div", { className: "muted mx-count" }, "the library search the cousin uses (cousin-memory search): keyword always, meaning when configured; a console search is not recorded as the cousin's recall", res && /* @__PURE__ */ React.createElement(React.Fragment, null, " · semantic ", semantic)), res && res.notice && /* @__PURE__ */ React.createElement("div", { className: "mx-toast bad" }, res.notice), err && /* @__PURE__ */ React.createElement("div", { className: "mx-toast bad" }, err)), /* @__PURE__ */ React.createElement("div", { className: "mx-list" }, res && res.hits.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no matches"), res && res.hits.map((h, i) => /* @__PURE__ */ React.createElement("div", { key: h.rel + i, className: "mx-entry", "data-search-hit": h.collection }, /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, i + 1, "."), /* @__PURE__ */ React.createElement("span", { className: "pill " + (h.legs.includes("semantic") ? "violet" : "blue"), "data-legs": h.legs.join(",") }, hitLegsText(h.legs)), /* @__PURE__ */ React.createElement("span", { className: "pill gray" }, h.collection), h.entry && /* @__PURE__ */ React.createElement(LevelPill, { level: h.entry.level }), /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, h.entry ? h.entry.topic : h.rel), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), h.similarity != null && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "similarity ", Number(h.similarity).toFixed(3))), /* @__PURE__ */ React.createElement("div", { className: "mx-content" }, h.entry ? h.entry.content : h.snippet), /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, h.rel), h.entry && h.entry.timestamp && /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(h.entry.timestamp)), h.entry && h.entry.cite && /* @__PURE__ */ React.createElement("span", null, "cite · ", /* @__PURE__ */ React.createElement("b", null, h.entry.cite)), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), h.entry && h.entry.topic && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => onHistory(h.entry.topic) }, "history"), !h.entry && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => setOpen(open === h.rel ? null : h.rel) }, open === h.rel ? "close" : "open")), open === h.rel && !h.entry && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", minHeight: 240, maxHeight: "60vh" } }, /* @__PURE__ */ React.createElement(
    FileViewer,
    {
      readUrl: `/api/memory/${slug}/file?path=${encodeURIComponent(h.rel)}${h.layer === "harness" ? "&layer=harness" : ""}`,
      path: h.rel
    }
  ))))));
}
function MemoryWrite({ slug, flash, onDone }) {
  const [writer, , writerErr] = useMemoryJson(`/api/memory/${slug}/writer`, 0);
  const [kind, setKind] = React.useState("remember");
  const [form, setForm] = React.useState({ topic: "", fact: "", decision: "", reasoning: "", level: "conclusion", note: "", scope: "", valid_until: "" });
  const [busy, setBusy] = React.useState(false);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const levels = writer && writer.levels || ["conclusion"];
  React.useEffect(() => {
    if (writer && !levels.includes(form.level)) setForm((f) => ({ ...f, level: "conclusion" }));
  }, [writer]);
  const ready = form.topic.trim() && (kind === "remember" ? form.fact.trim() : form.decision.trim() && form.reasoning.trim());
  const submit = async () => {
    if (!ready || busy) return;
    setBusy(true);
    const body = kind === "remember" ? {
      topic: form.topic,
      fact: form.fact,
      level: form.level,
      note: form.note,
      ...form.scope.trim() ? { scope: form.scope } : {},
      ...form.valid_until.trim() ? { valid_until: form.valid_until } : {}
    } : { topic: form.topic, decision: form.decision, reasoning: form.reasoning, level: form.level, note: form.note };
    const url = kind === "remember" ? `/api/memory/${slug}/remember` : `/api/memory/${slug}/decide`;
    const { r, d } = await safeSend("POST", url, body);
    setBusy(false);
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `not written: ${d.error || r.status}` });
      return;
    }
    flash({ ok: true, msg: d.line || "written" });
    setForm((f) => ({ ...f, fact: "", decision: "", reasoning: "", note: "", scope: "", valid_until: "" }));
    onDone && onDone();
  };
  const who = writer ? writer.user ? `console user ${writer.user}` : "console (no login)" : "...";
  return /* @__PURE__ */ React.createElement("div", { className: "mx-cards", "data-memory-write": true }, /* @__PURE__ */ React.createElement("div", { className: "panel mx-card", style: { gridColumn: "1 / -1" } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "write to @", slug, "'s memory"), writerErr && /* @__PURE__ */ React.createElement("span", { className: "muted" }, writerErr), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("div", { className: "radio-row", "data-write-kind": kind }, /* @__PURE__ */ React.createElement("button", { className: kind === "remember" ? "sel" : "", onClick: () => setKind("remember") }, "remember"), /* @__PURE__ */ React.createElement("button", { className: kind === "decide" ? "sel" : "", onClick: () => setKind("decide") }, "decide"))), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "topic"), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.topic, onChange: set("topic"), placeholder: "one topic per fact, as the cousin files them" })), kind === "remember" ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "fact"), /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: form.fact, onChange: set("fact") })), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("div", { className: "field", style: { flex: "2 1 220px" } }, /* @__PURE__ */ React.createElement("label", null, "scope (optional)"), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: form.scope,
      onChange: set("scope"),
      maxLength: 200,
      "data-write-scope": true,
      placeholder: "what it holds for: board rev A, the main office"
    }
  )), /* @__PURE__ */ React.createElement("div", { className: "field", style: { flex: "1 1 160px" } }, /* @__PURE__ */ React.createElement("label", null, "valid until (optional)"), /* @__PURE__ */ React.createElement("input", { className: "txt", type: "date", value: form.valid_until, onChange: set("valid_until"), "data-write-valid-until": true }), /* @__PURE__ */ React.createElement("span", { className: "hint" }, "through that day; then it leaves the views and recall says expired")))) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "decision"), /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: form.decision, onChange: set("decision") })), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "why"), /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: form.reasoning, onChange: set("reasoning") }))), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "truth level"), /* @__PURE__ */ React.createElement("select", { className: "sel", value: form.level, onChange: set("level"), "data-write-levels": levels.join(",") }, levels.map((l) => /* @__PURE__ */ React.createElement("option", { key: l, value: l }, l))), /* @__PURE__ */ React.createElement("span", { className: "hint" }, writer && writer.can_write_operator ? "operator: you are this cousin's operator, so you may state it as the operator's word" : `operator level is the operator account's only (${writer && writer.operator || "no [operator] name set"}); framework entries are the framework's, and obsolete is the retire action`)), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "note for the cite (optional)"), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.note, onChange: set("note"), placeholder: "where it came from: a chat, a call, a document", maxLength: 300 }), /* @__PURE__ */ React.createElement("span", { className: "hint", "data-cite-preview": true }, "cite, filled by the console: ", /* @__PURE__ */ React.createElement("b", null, who, ", <the time of writing>", form.note.trim() ? "; " + form.note.trim() : ""))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8 } }, /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !ready || busy, onClick: submit }, kind === "remember" ? "remember" : "log the decision")))));
}
function ReviewQueue({ slug, reload, flash, onChanged }) {
  const [data, load, err] = useMemoryJson(`/api/memory/${slug}/review`, reload);
  const [writer] = useMemoryJson(`/api/memory/${slug}/writer`, 0);
  const [op] = useLongOp(slug);
  const [marks, setMarks] = React.useState({});
  const [why, setWhy] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    setMarks({});
  }, [slug, reload]);
  const reviewOp = op && op.kind === "memory-review" ? op : null;
  const running = !!(op && op.status === "running");
  const seen = React.useRef(null);
  React.useEffect(() => {
    if (!reviewOp) return;
    const key = reviewOp.id + ":" + reviewOp.status;
    if (seen.current && seen.current !== key && reviewOp.status !== "running") {
      if (reviewOp.status === "done" && reviewOp.result) report(reviewOp.result);
      else flash({ ok: false, msg: `review failed: ${reviewOp.error || "see the console log"}` });
      load();
      onChanged && onChanged();
    }
    seen.current = key;
  }, [reviewOp && reviewOp.id, reviewOp && reviewOp.status]);
  const held = data && data.held || [];
  const body = reviewBody(marks, why);
  const n = Object.keys(body.verdicts).length;
  const drops = Object.values(body.verdicts).filter((v) => v === "drop").length;
  const markAll = (v) => setMarks(Object.fromEntries(held.map((r) => [r.id, v])));
  const report = (d) => {
    const errs = Object.entries(d.errors || {});
    flash({ ok: errs.length === 0, msg: `${Object.keys(d.done || {}).length} settled` + (errs.length ? ` · left held: ${errs.map(([k, v]) => `${k} (${v})`).join("; ")}` : "") });
  };
  const apply = async () => {
    if (!n || busy || running) return;
    setBusy(true);
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/review`, body);
    setBusy(false);
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `review not applied: ${d.error || r.status}` });
      return;
    }
    setMarks({});
    setWhy("");
    if (r.status === 202) {
      flash({ ok: true, msg: `settling ${n} as @${slug}'s long operation...` });
      seen.current = "pending";
      return;
    }
    report(d);
    load();
    onChanged && onChanged();
  };
  const loggedIn = writer && writer.user;
  const blocked = busy || running || !loggedIn;
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap", "data-review-queue": true }, /* @__PURE__ */ React.createElement("div", { className: "mx-hint" }, "the review gate holds a batch of new entries (over [memory] review_batch = ", data ? data.batch : "?", " at once) out of every view until someone keeps or drops each. A drop is an entry-level obsolete mark and has no undo; an operator-level entry is dropped by the operator account only (", data && data.operator || "none set", ").", writer && !loggedIn && /* @__PURE__ */ React.createElement(React.Fragment, null, " ", /* @__PURE__ */ React.createElement("b", null, "A verdict needs a logged-in console user"), ": without logins the console cannot tell you from a cousin.")), reviewOp && /* @__PURE__ */ React.createElement(LongOpStatus, { slug, kind: "memory-review" }), /* @__PURE__ */ React.createElement("div", { className: "mx-inputs" }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: !held.length || running, onClick: () => markAll("keep") }, "mark all keep"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: !held.length || running, onClick: () => markAll("drop") }, "mark all drop"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: !n, onClick: () => setMarks({}) }, "clear"), /* @__PURE__ */ React.createElement("input", { placeholder: "why (optional, recorded with each verdict)", value: why, maxLength: 500, onChange: (e) => setWhy(e.target.value), style: { flex: 1 } }), drops > 0 ? /* @__PURE__ */ React.createElement(ConfirmButton, { className: "btn danger", disabled: blocked, label: `apply ${n} (${drops} drop)`, confirmLabel: `confirm: drop ${drops}, no undo`, onConfirm: apply }) : /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !n || blocked, onClick: apply }, "apply ", n || "")), /* @__PURE__ */ React.createElement("div", { className: "mx-list" }, /* @__PURE__ */ React.createElement(Pending, { data, err }), data && held.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "nothing held for review"), held.map((r) => /* @__PURE__ */ React.createElement("div", { key: r.id, className: "mx-entry lvl-" + LEVEL_SHORT(normLevel(r.truth_level)), "data-held": r.id }, /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement(LevelPill, { level: normLevel(r.truth_level) }), /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, r.topic), /* @__PURE__ */ React.createElement("span", { className: "muted" }, r.id), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(r.valid_from))), /* @__PURE__ */ React.createElement("div", { className: "mx-content" }, r.content), /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, r.source && /* @__PURE__ */ React.createElement("span", null, "source · ", /* @__PURE__ */ React.createElement("b", null, r.source)), r.cite && /* @__PURE__ */ React.createElement("span", null, "cite · ", /* @__PURE__ */ React.createElement("b", null, r.cite)), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("div", { className: "radio-row", "data-verdict": marks[r.id] || "held" }, ["keep", "drop"].map((v) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: v,
      className: marks[r.id] === v ? "sel" : "",
      disabled: running,
      onClick: () => setMarks((m) => ({ ...m, [r.id]: m[r.id] === v ? void 0 : v }))
    },
    v
  ))))))));
}
const MAINTENANCE = [
  { action: "distill", title: "distill", what: "rebuild memory/distilled/ from raw (cousin-memory distill). The boot packet reads it; the loops daemon also catches up within a tick." },
  { action: "compact-raw", title: "compact raw", what: "fold daily raw files older than the hot window into monthly gzip archives and a per-topic digest (cousin-memory compact --target raw). Lossless." },
  { action: "compact-index", title: "compact the index", what: "retire the oldest MEMORY.md pointers until it fits its byte budget (cousin-memory compact). A snapshot is kept; preview first." },
  { action: "reindex", title: "reindex", what: "rebuild the keyword index, and the semantic one when config/embedding.toml sets it up (cousin-memory reindex). Embedding can take a while." }
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
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/maintain`, { action, ...extra || {} });
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `${action} not started: ${d.error || r.status}` });
      return;
    }
    reload();
  };
  const ins = ov && ov.insights || {};
  const state = {
    distill: ins.distilled_behind_raw ? "raw has entries newer than the last distill" : "current",
    "compact-raw": ins.pending_fold_files ? `${ins.pending_fold_files} daily file(s) past the fold window` : "nothing past the fold window",
    "compact-index": "",
    reindex: ""
  };
  const result = mine && mine.result;
  return /* @__PURE__ */ React.createElement("div", { className: "mx-cards", "data-memory-maintenance": true }, mine && /* @__PURE__ */ React.createElement("div", { className: "panel mx-card", style: { gridColumn: "1 / -1" } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "last run")), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, /* @__PURE__ */ React.createElement(LongOpStatus, { slug, kind: mine.kind }), result && result.report && result.report.would_retire && /* @__PURE__ */ React.createElement("div", { className: "mx-sub" }, mine.params && mine.params.dry_run ? "would retire" : "retired", ": ", (mine.params && mine.params.dry_run ? result.report.would_retire : result.report.retired).join(", ") || "nothing (the index fits its budget)"))), op && running && !mine && /* @__PURE__ */ React.createElement("div", { className: "mx-toast bad", style: { gridColumn: "1 / -1" } }, "a ", op.kind, " is running on @", slug, "; maintenance waits for it"), MAINTENANCE.map((m) => /* @__PURE__ */ React.createElement("div", { key: m.action, className: "panel mx-card", "data-maintain": m.action }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, m.title), state[m.action] && /* @__PURE__ */ React.createElement("span", { className: "muted" }, state[m.action])), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { className: "mx-sub", style: { marginTop: 0 } }, m.what), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, justifyContent: "flex-end" } }, m.action === "compact-index" ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: running, onClick: () => start(m.action, { dry_run: true }) }, "preview"), /* @__PURE__ */ React.createElement(
    ConfirmButton,
    {
      className: "btn danger",
      label: "compact",
      confirmLabel: "confirm: retire pointers",
      disabled: running,
      onConfirm: () => start(m.action)
    }
  )) : /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: running, onClick: () => start(m.action) }, m.title, " now"))))));
}
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
    if (last.current && last.current !== key && mine.status !== "running") {
      setTick((t) => t + 1);
      onChanged && onChanged();
    }
    last.current = key;
  }, [mine && mine.id, mine && mine.status]);
  if (!data) return /* @__PURE__ */ React.createElement(Pending, { data, err });
  const cfg = data.settings || {};
  const run = async () => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/dreams/run`, {});
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `dreaming not started: ${d.error || r.status}` });
      return;
    }
    reloadOp();
  };
  const undo = async (passId) => {
    const { r, d } = await safeSend("POST", `/api/memory/${slug}/dreams/undo`, { pass_id: passId });
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `undo failed: ${d.error || r.status}` });
      return;
    }
    flash({ ok: true, msg: `pass ${passId} undone: ${(d.reverted || []).length} change(s) reversed` });
    setTick((t) => t + 1);
    onChanged && onChanged();
  };
  const passes = data.passes || [];
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap", "data-dreams": true }, /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "setting"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, cfg.mode === "off" ? "off" : cfg.mode === "nightly" ? `nightly at ${cfg.at}` : "after each rollover")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { className: "mx-sub", style: { marginTop: 0 } }, "A pass is a short Sonnet session with memory tools only (no files, no shell), up to 64k tokens; the first pass starts 30 days back. It merges duplicates, retires stale claims and settles contradictions, never touching L0-L2. Turn it on or off in the cousin's Agent settings (dreaming, dreaming_at)."), mine && /* @__PURE__ */ React.createElement(LongOpStatus, { slug, kind: "memory-dream" }), operator && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: running, onClick: run }, "dream now")))), passes.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no passes yet"), passes.map((p) => /* @__PURE__ */ React.createElement("div", { key: p.pass_id, className: "panel mx-card", "data-dream": p.pass_id }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, fmtWhen(p.started), " · ", p.trigger), /* @__PURE__ */ React.createElement("span", { className: "muted " + (DREAM_RESULT_TONE[p.result] || "") }, p.result, p.undone ? " · undone" : "", " · ", (p.changes || []).length, " change(s)", p.tokens != null ? ` · ${p.tokens} tokens` : "")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 6 } }, p.summary && /* @__PURE__ */ React.createElement("div", { className: "mx-sub", style: { marginTop: 0 } }, p.summary), p.error && /* @__PURE__ */ React.createElement("div", { className: "mx-sub bad", style: { marginTop: 0 } }, p.error), (p.changes || []).map((c, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "mx-sub", style: { marginTop: 0, fontFamily: "var(--mono)" } }, c.op, " · ", c.topic || "-", c.why ? ` · ${c.why}` : "", (c.entry_ids || []).length ? ` · ${(c.entry_ids || []).join(", ")}` : "")), operator && (p.changes || []).length > 0 && !p.undone && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement(
    ConfirmButton,
    {
      className: "btn danger",
      label: "undo",
      confirmLabel: "confirm: reverse this pass",
      onConfirm: () => undo(p.pass_id)
    }
  ))))));
}
function SelfPortrait({ slug, flash, onChanged }) {
  const [st, load, err] = useMemoryJson(`/api/memory/${slug}/portrait`, 0);
  const [tab, setTab] = React.useState("diff");
  const [draft, setDraft] = React.useState(null);
  const [typed, setTyped] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    setDraft(null);
    setTyped("");
  }, [slug]);
  React.useEffect(() => {
    if (st && draft === null) setDraft(st.candidate || "");
  }, [st]);
  const dirty = !!st && draft !== null && draft !== (st.candidate || "");
  const urls = {
    synthesize: `/api/memory/${slug}/portrait/synthesize`,
    candidate: `/api/memory/${slug}/portrait/candidate`,
    commit: `/api/memory/${slug}/portrait/commit`
  };
  const call = async (what, body, okMsg) => {
    setBusy(true);
    const { r, d } = await safeSend("POST", urls[what], body || {});
    setBusy(false);
    if (!r.ok || !d.ok) {
      flash({ ok: false, msg: `${what}: ${d.error || r.status}` });
      if (r.status === 409) load();
      return null;
    }
    flash({ ok: true, msg: okMsg });
    setDraft(d.candidate || "");
    await load();
    onChanged && onChanged();
    return d;
  };
  if (!st) return /* @__PURE__ */ React.createElement(Pending, { data: st, err });
  const ready = portraitCommitReady(typed, slug, st.candidate_sha, dirty);
  return /* @__PURE__ */ React.createElement("div", { className: "mx-list-wrap", "data-self-portrait": true }, /* @__PURE__ */ React.createElement("div", { className: "mx-hint" }, "the self-portrait is a reviewed identity layer: the boot packet reads only the committed one. Draft a candidate from the cousin's own sources, edit it, then commit. Committing is an identity gate: a logged-in person types the slug back; the previous portrait is kept as .self-portrait.md.bak."), /* @__PURE__ */ React.createElement("div", { className: "mx-inputs" }, /* @__PURE__ */ React.createElement("div", { className: "radio-row", "data-portrait-tab": tab }, ["diff", "candidate", "committed"].map((t) => /* @__PURE__ */ React.createElement("button", { key: t, className: tab === t ? "sel" : "", onClick: () => setTab(t) }, t))), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "candidate ", st.candidate_exists ? st.candidate_sha : "none", " · committed ", st.committed_exists ? "yes" : "none"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), st.candidate_exists ? /* @__PURE__ */ React.createElement(
    ConfirmButton,
    {
      className: "btn",
      label: "draft again",
      confirmLabel: "confirm: replace the candidate",
      disabled: busy,
      onConfirm: () => call("synthesize", { replace: true }, "candidate drafted again")
    }
  ) : /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: busy, onClick: () => call("synthesize", {}, "candidate drafted") }, "draft a candidate")), /* @__PURE__ */ React.createElement("div", { style: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "auto" } }, tab === "diff" && (st.candidate_exists ? /* @__PURE__ */ React.createElement("pre", { className: "mx-lines", "data-portrait-diff": true, style: { padding: 12, whiteSpace: "pre-wrap" } }, st.diff || "(no difference from the committed portrait)") : /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no candidate: draft one, or write one in the candidate tab")), tab === "committed" && (st.committed_exists ? /* @__PURE__ */ React.createElement("div", { style: { padding: 12 } }, /* @__PURE__ */ React.createElement(MarkdownDoc, { text: st.committed })) : /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no committed self-portrait yet")), tab === "candidate" && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8, flex: 1 } }, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt code",
      "data-portrait-editor": true,
      value: draft || "",
      onChange: (e) => setDraft(e.target.value),
      style: { flex: 1, minHeight: 320 },
      spellCheck: false
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, dirty ? "unsaved edits" : "saved"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: !dirty || busy, onClick: () => setDraft(st.candidate || "") }, "discard edits"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn primary",
      disabled: !dirty || busy || !(draft || "").trim(),
      onClick: () => call("candidate", { text: draft }, "candidate saved")
    },
    "save the candidate"
  )))), /* @__PURE__ */ React.createElement("div", { className: "panel", "data-portrait-commit": true }, /* @__PURE__ */ React.createElement("div", { className: "panel-body mx-inputs" }, /* @__PURE__ */ React.createElement("span", null, "type ", /* @__PURE__ */ React.createElement("b", null, slug), " to commit candidate ", st.candidate_sha || "-"), /* @__PURE__ */ React.createElement("input", { value: typed, onChange: (e) => setTyped(e.target.value), placeholder: slug, autoComplete: "off", spellCheck: false }), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn danger",
      disabled: !ready || busy,
      onClick: async () => {
        if (await call("commit", { confirm: typed, sha: st.candidate_sha }, "self-portrait committed")) setTyped("");
      }
    },
    "commit"
  ), dirty && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "save or discard the edits first"))));
}
function MomentsList({ slug, reload }) {
  const [cb, , cbErr] = useMemoryJson(`/api/memory/${slug}/callbacks`, reload);
  const [cap, , capErr] = useMemoryJson(`/api/memory/${slug}/capsules`, reload);
  const list = (v) => Array.isArray(v) ? v : v == null || v === "" ? [] : [String(v)];
  return /* @__PURE__ */ React.createElement("div", { className: "mx-cards", "data-memory-moments": true }, /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "reasoning capsules"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "newest first · read-only (cousin-reason)")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement(Pending, { data: cap, err: capErr }), cap && cap.capsules.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no capsules"), (cap ? cap.capsules : []).map((c) => /* @__PURE__ */ React.createElement("div", { key: c.id, className: "mx-entry", "data-capsule": c.id }, /* @__PURE__ */ React.createElement("div", { className: "mx-entry-h" }, /* @__PURE__ */ React.createElement("span", { className: "mx-topic" }, c.conclusion), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "pill gray" }, c.confidence)), list(c.evidence).length > 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-content" }, /* @__PURE__ */ React.createElement("b", null, "evidence:"), " ", list(c.evidence).join(" · ")), list(c.rejected).length > 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-content mx-why" }, /* @__PURE__ */ React.createElement("b", null, "rejected:"), " ", list(c.rejected).join(" · ")), /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, c.topic && /* @__PURE__ */ React.createElement("span", null, "topic · ", /* @__PURE__ */ React.createElement("b", null, c.topic)), /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(c.timestamp))))))), /* @__PURE__ */ React.createElement("div", { className: "panel mx-card" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "callbacks"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "newest first · read-only (cousin-callback)")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement(Pending, { data: cb, err: cbErr }), cb && cb.callbacks.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "no callbacks"), (cb ? cb.callbacks : []).map((m, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "mx-meta", "data-callback": true }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtStamp(m.time)), m.category && /* @__PURE__ */ React.createElement("span", { className: "pill gray" }, m.category), /* @__PURE__ */ React.createElement("span", { className: "mx-content" }, m.moment))))));
}
function SharedReviewersPanel({ onChange }) {
  const [st, setSt] = React.useState(null);
  const [edit, setEdit] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const load = React.useCallback(async () => {
    const { r, d } = await safeSend("GET", "/api/shared/reviewers");
    if (!r.ok) {
      setMsg({ ok: false, msg: `reviewers: ${d.error || r.status}` });
      return;
    }
    setSt(d);
    if (onChange) onChange(d);
  }, [onChange]);
  React.useEffect(() => {
    load();
  }, [load]);
  const save = async () => {
    const reviewers = (edit || "").split("\n").map((s) => s.trim()).filter(Boolean);
    const { r, d } = await safeSend("POST", "/api/shared/reviewers", { reviewers });
    if (!r.ok || !d.ok) {
      setMsg({ ok: false, msg: d.error || `HTTP ${r.status}` });
      return;
    }
    setMsg({ ok: true, msg: `reviewers saved: ${d.reviewers.join(", ") || "none"}` });
    setEdit(null);
    load();
  };
  if (!st) return msg ? /* @__PURE__ */ React.createElement("div", { className: "mx-toast bad" }, msg.msg) : null;
  return /* @__PURE__ */ React.createElement("div", { className: "panel", style: { flex: "0 0 auto" }, "data-shared-reviewers": true }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "reviewers"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, st.configured ? st.reviewers.length : "not configured"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), edit === null && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      disabled: !st.can_edit,
      title: st.can_edit ? "change who may promote into the shared tier" : st.user ? "only a current reviewer changes the list" : "needs a logged-in console user",
      onClick: () => {
        setEdit(st.reviewers.join("\n"));
        setMsg(null);
      }
    },
    "edit"
  )), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 6 } }, st.error && /* @__PURE__ */ React.createElement("div", { className: "mx-toast bad" }, "config/shared-reviewers.json is ", st.error, "; fix it by hand"), edit === null ? /* @__PURE__ */ React.createElement("div", { className: "mx-meta" }, st.reviewers.map((r) => /* @__PURE__ */ React.createElement("span", { key: r, className: "pill gray" }, r)), st.reviewers.length === 0 && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "nobody may promote yet"), st.user && st.you_review !== null && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "you (", st.user, ") ", st.you_review ? "review" : "do not review")) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("textarea", { className: "txt code", value: edit, onChange: (e) => setEdit(e.target.value), placeholder: "one name per line", rows: 4 }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => setEdit(null) }, "cancel"), /* @__PURE__ */ React.createElement(ConfirmButton, { className: "btn danger", label: "save", confirmLabel: "confirm: change the reviewers", onConfirm: save }))), msg && /* @__PURE__ */ React.createElement("div", { className: "mx-toast " + (msg.ok ? "ok" : "bad") }, msg.msg)));
}
function FileTreeNode({ slug, entry, depth, showHidden, selected, onSelect }) {
  const [open, setOpen] = React.useState(false);
  const [kids, setKids] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const isDir = entry.type === "dir" || entry.type === "link" && entry.target_type === "dir" && !entry.outside;
  const toggle = async () => {
    if (!isDir) {
      if (!(entry.type === "link" && entry.outside)) onSelect(entry);
      return;
    }
    const next = !open;
    setOpen(next);
    if (next) {
      const d = await apiGet(`/api/cousins/${slug}/files?path=${encodeURIComponent(entry.path)}${showHidden ? "&hidden=1" : ""}`);
      if (d) {
        setKids(d.entries);
        setErr(d.truncated ? `showing ${d.entries.length} of ${d.total}` : null);
      } else setErr("unreadable");
    }
  };
  React.useEffect(() => {
    setKids(null);
    setOpen(false);
  }, [showHidden]);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "ft-row" + (selected === entry.path ? " sel" : "") + (entry.outside ? " outside" : ""),
      style: { paddingLeft: 8 + depth * 14 },
      onClick: toggle,
      "data-path": entry.path,
      title: entry.outside ? "link that leaves the home: not followed" : entry.path
    },
    /* @__PURE__ */ React.createElement("span", { className: "ft-caret" }, isDir ? open ? "v" : ">" : ""),
    /* @__PURE__ */ React.createElement("span", { className: "ft-name" + (isDir ? " dir" : "") }, entry.name, entry.type === "link" ? " @" : ""),
    /* @__PURE__ */ React.createElement("span", { className: "ft-size" }, isDir ? "" : fmtBytes(entry.size)),
    /* @__PURE__ */ React.createElement("span", { className: "ft-time" }, fmtWhen(entry.mtime))
  ), open && err && /* @__PURE__ */ React.createElement("div", { className: "ft-row muted", style: { paddingLeft: 22 + depth * 14 } }, err), open && kids && kids.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "ft-row muted", style: { paddingLeft: 22 + depth * 14 } }, "(empty)"), open && kids && kids.map((k) => /* @__PURE__ */ React.createElement(FileTreeNode, { key: k.path, slug, entry: k, depth: depth + 1, showHidden, selected, onSelect })));
}
function CousinFiles({ slug }) {
  const [showHidden, setShowHidden] = React.useState(false);
  const [root, setRoot] = React.useState(null);
  const [sel, setSel] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet(`/api/cousins/${slug}/files${showHidden ? "?hidden=1" : ""}`).then((d) => {
      if (!cancelled) setRoot(d);
    });
    return () => {
      cancelled = true;
    };
  }, [slug, showHidden]);
  return /* @__PURE__ */ React.createElement("div", { className: "ft-wrap", "data-file-explorer": slug }, /* @__PURE__ */ React.createElement("div", { className: "ft-tree" }, /* @__PURE__ */ React.createElement("div", { className: "ft-bar" }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, "~/", slug), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("label", { className: "muted" }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: showHidden, onChange: (e) => setShowHidden(e.target.checked) }), " dotfiles")), !root && /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "loading..."), root && root.entries.map((e) => /* @__PURE__ */ React.createElement(FileTreeNode, { key: e.path + showHidden, slug, entry: e, depth: 0, showHidden, selected: sel && sel.path, onSelect: setSel }))), /* @__PURE__ */ React.createElement("div", { className: "ft-view" }, sel ? /* @__PURE__ */ React.createElement(
    FileViewer,
    {
      key: sel.path,
      readUrl: `/api/cousins/${slug}/files/read?path=${encodeURIComponent(sel.path)}`,
      downloadUrl: `/api/cousins/${slug}/files/download?path=${encodeURIComponent(sel.path)}`,
      path: sel.path
    }
  ) : /* @__PURE__ */ React.createElement("div", { className: "mx-empty" }, "select a file; .secrets/ is never shown")));
}
function CousinFilesModal({ slug, onClose }) {
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return ReactDOM.createPortal(/* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal ft-modal", onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "files · @", slug), /* @__PURE__ */ React.createElement("span", { style: { marginLeft: "auto", color: "var(--fg-3)", fontSize: 10 } }, "read-only"), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose }, "×")), /* @__PURE__ */ React.createElement("div", { className: "body", style: { padding: 0, flex: 1, minHeight: 0, display: "flex" } }, /* @__PURE__ */ React.createElement(CousinFiles, { slug })))), document.body);
}
Object.assign(window, {
  MemoryExplorer,
  CousinFiles,
  CousinFilesModal,
  FileViewer,
  MarkdownDoc,
  sanitizeHtml,
  ConfirmButton,
  fmtBytes,
  groupByLevel,
  entryCite,
  LEVEL_META,
  SharedReviewersPanel,
  hitLegsText,
  reviewBody,
  claimRetireBody,
  portraitCommitReady
});
