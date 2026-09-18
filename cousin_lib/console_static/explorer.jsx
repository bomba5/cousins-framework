// explorer.jsx: the cousin file explorer and the file viewer it shares.
// It reads through confined routes (docs/console-spec.md, "Cousin
// files"); nothing here decides what is safe to show.

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
  CousinFiles, CousinFilesModal, FileViewer, MarkdownDoc, sanitizeHtml,
  fmtBytes,
});
