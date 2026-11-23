// Language: this package's own file (index.html's package block). Its routes
// are cousin_lib/console/routes_language.py; the page is docs/plain-language.md.
//
// A view (registerView "language") with tabs:
//   check     a text box, "Check" (POST /api/language/check) and the findings
//             by line, rule and message; "Fix" shows the text with the em
//             dashes fixed.
//   settings  config/plain-language.toml (GET/PUT /api/language/config), checked
//             by the server before it is written, and each cousin's reply mode,
//             written as a cousin setting (POST /api/cousins/<slug>/settings).
//   glossary  the words a repository uses (cousin_lib/glossary.py): "Find terms"
//             (GET /api/language/terms), "Check groups" (POST /api/language/groups)
//             and "Derive" (POST /api/language/derive), which shows the derived
//             glossary to copy; the console writes no file. The server refuses a
//             path outside the install, the cousins' homes and the checkout.

// ---- pure helpers (the tests run this block under node) -------------------
// The banned list as the settings box shows it: one "word = use" per line.
function languageBannedText(list) {
  return (list || []).map(b => b.use ? `${b.word} = ${b.use}` : b.word).join("\n");
}

// The box's text back as [{word, use}]: blank lines dropped, "use" optional.
function languageBannedParse(text) {
  return String(text || "").split("\n").map(l => l.trim()).filter(Boolean).map(l => {
    const at = l.indexOf("=");
    if (at < 0) return { word: l, use: "" };
    return { word: l.slice(0, at).trim(), use: l.slice(at + 1).trim() };
  }).filter(b => b.word);
}

// The groups box: one synonym group per line, "preferred, other, other".
function languageGroupsParse(text) {
  return String(text || "").split("\n")
    .map(l => l.split(",").map(w => w.trim()).filter(Boolean).join(","))
    .filter(Boolean);
}
// ---- end pure helpers -------------------------------------------------------

const LANG_MONO = { fontFamily: "var(--mono)", fontSize: 12 };
const LANG_PRE = { ...LANG_MONO, whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto",
                   background: "var(--bg-0)", padding: 10, borderRadius: 6, margin: 0 };
const LANG_TABS = [["check", "check"], ["glossary", "glossary"], ["settings", "settings"]];

async function langSend(method, path, body) {
  const { r, d } = await apiSend(method, path, body);
  if (!r.ok) throw new Error((d && d.error) || `HTTP ${r.status}`);
  return d;
}

function LanguageFindings({ findings }) {
  if (!findings) return null;
  if (!findings.length) return <div className="muted" style={LANG_MONO}>no findings</div>;
  return <table className="data" data-language-findings>
    <thead><tr><th>line</th><th>rule</th><th>message</th><th>text</th></tr></thead>
    <tbody>
      {findings.map((f, i) => <tr key={i}>
        <td style={LANG_MONO}>{f.line}:{f.col}</td>
        <td><span className={`pill ${f.severity === "note" ? "gray" : "amber"}`}>{f.rule}</span></td>
        <td>{f.message}</td>
        <td style={LANG_MONO}>{f.excerpt}</td>
      </tr>)}
    </tbody>
  </table>;
}

function LanguageCheck() {
  const [text, setText] = React.useState("");
  const [glossary, setGlossary] = React.useState("");
  const [result, setResult] = React.useState(null);
  const [showFixed, setShowFixed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const run = async () => {
    setBusy(true); setErr(null); setShowFixed(false);
    try {
      const body = { text };
      if (glossary.trim()) body.glossary = glossary.trim();
      setResult(await langSend("POST", "/api/language/check", body));
    } catch (e) { setErr(String(e.message || e)); setResult(null); } finally { setBusy(false); }
  };
  const fixable = result ? result.findings.filter(f => f.fix != null).length : 0;
  return <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
    <textarea className="txt" rows={12} value={text} onChange={e => setText(e.target.value)}
              placeholder="paste the text or Markdown to check" style={{ ...LANG_MONO, width: "100%" }} />
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <input className="txt" value={glossary} onChange={e => setGlossary(e.target.value)}
             placeholder="glossary file (optional; else the configured one)" style={{ ...LANG_MONO, flex: 1, minWidth: 240 }} />
      <button className="btn primary" disabled={busy || !text.trim()} onClick={run}>Check</button>
      <button className="btn" disabled={!result || !fixable} onClick={() => setShowFixed(s => !s)}
              title="the text with each fixable finding (an em dash) fixed">{showFixed ? "Hide fix" : "Fix"}</button>
    </div>
    {err && <div style={{ color: "var(--red)" }}>{err}</div>}
    {result && <div className="muted" style={LANG_MONO}>
      {result.findings.length} finding{result.findings.length === 1 ? "" : "s"}, {fixable} fixable,
      glossary of {result.glossary_terms} term{result.glossary_terms === 1 ? "" : "s"}
    </div>}
    {showFixed && result && <pre style={LANG_PRE} data-language-fixed>{result.fixed}</pre>}
    {result && <LanguageFindings findings={result.findings} />}
  </div>;
}

function LanguageReplyModes({ cousins, modes, installDefault, onSaved }) {
  const [busy, setBusy] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const set = async (slug, value) => {
    setBusy(slug); setErr(null);
    try {
      await langSend("POST", `/api/cousins/${encodeURIComponent(slug)}/settings`,
                     { changes: { "plain_language.reply": value === "" ? null : value } });
      onSaved();
    } catch (e) { setErr(`${slug}: ${String(e.message || e)}`); } finally { setBusy(null); }
  };
  return <div data-language-reply>
    <SectionLabel style={{ marginTop: 20 }}>reply mode per cousin</SectionLabel>
    <div className="muted" style={{ marginBottom: 6 }}>
      What a cousin's reply and send do with the check: off; note (the text goes as written, the
      findings come back to the cousin); fix (em dashes fixed before it goes, the rest noted). It never
      blocks a reply. Applies from the next reply, without a restart.
    </div>
    {err && <div style={{ color: "var(--red)" }}>{err}</div>}
    <table className="data">
      <thead><tr><th>cousin</th><th>reply</th></tr></thead>
      <tbody>
        {cousins.map(c => <tr key={c.slug}>
          <td>{c.name} <span className="muted" style={LANG_MONO}>{c.slug}</span></td>
          <td>
            <select className="sel" value={c.set ? c.reply : ""} disabled={busy === c.slug}
                    onChange={e => set(c.slug, e.target.value)}>
              <option value="">install default ({installDefault})</option>
              {modes.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </td>
        </tr>)}
      </tbody>
    </table>
  </div>;
}

function LanguageSettings() {
  const [data, setData] = React.useState(null);
  const [draft, setDraft] = React.useState(null);
  const [banned, setBanned] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const load = async () => {
    const d = await apiGet("/api/language/config");
    if (!d) { setErr("could not read the config"); return; }
    setData(d); setDraft(d.config); setBanned(languageBannedText(d.config.banned));
  };
  React.useEffect(() => { load(); }, []);
  if (!data || !draft) return <div className="muted">{err || "loading..."}</div>;
  const put = (k, v) => setDraft(Object.assign({}, draft, { [k]: v }));
  const save = async () => {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const d = await langSend("PUT", "/api/language/config",
                               { config: Object.assign({}, draft, { banned: languageBannedParse(banned) }) });
      setDraft(d.config); setBanned(languageBannedText(d.config.banned));
      setMsg(`saved to ${data.path}`);
      load();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };
  return <div style={{ display: "flex", flexDirection: "column", gap: 8 }} data-language-settings>
    <SectionLabel>{data.path}{data.exists ? "" : " (not written yet: the defaults)"}</SectionLabel>
    <div className="muted">Save writes the whole file from these values: comments in it are lost.</div>
    {data.error && <div style={{ color: "var(--red)" }}>the file does not check: {data.error}; saving replaces it</div>}
    <dl className="kv" style={{ gridTemplateColumns: "160px 1fr" }}>
      {["step", "description"].map(k => <React.Fragment key={k}>
        <dt>{k} limit</dt>
        <dd><input className="txt" type="number" min="1" max="1000" value={draft.limits[k]} disabled={busy}
                   style={{ width: 90 }} onChange={e => put("limits", Object.assign({}, draft.limits, { [k]: Number(e.target.value) }))} />
          <span className="muted" style={{ marginLeft: 8 }}>
            {k === "step" ? "words per sentence in a numbered item or one that starts with a verb"
                          : "words per sentence anywhere else"}
          </span></dd>
      </React.Fragment>)}
      <dt>rules</dt>
      <dd style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        {Object.keys(draft.rules).map(r => <label key={r} style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
          <input type="checkbox" checked={draft.rules[r]} disabled={busy}
                 onChange={e => put("rules", Object.assign({}, draft.rules, { [r]: e.target.checked }))} />
          <span style={LANG_MONO}>{r}</span>
        </label>)}
      </dd>
      <dt>banned words</dt>
      <dd><textarea className="txt" rows={5} value={banned} disabled={busy} onChange={e => setBanned(e.target.value)}
                    placeholder="leverage = use" style={{ ...LANG_MONO, width: "100%" }} />
        <span className="muted">one per line: the word or phrase, then = and the word to use instead</span></dd>
      <dt>glossary</dt>
      <dd><input className="txt" value={draft.glossary} disabled={busy} onChange={e => put("glossary", e.target.value)}
                 placeholder="a path, relative to the install root" style={{ ...LANG_MONO, width: "100%" }} /></dd>
      <dt>reply default</dt>
      <dd><select className="sel" value={draft.reply_default} disabled={busy} onChange={e => put("reply_default", e.target.value)}>
        {data.modes.map(m => <option key={m} value={m}>{m}</option>)}
      </select> <span className="muted">for a cousin whose cousin.toml sets no reply mode</span></dd>
    </dl>
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <button className="btn primary" disabled={busy} onClick={save}>Save</button>
      {msg && <span className="muted">{msg}</span>}
      {err && <span style={{ color: "var(--red)" }}>{err}</span>}
    </div>
    <LanguageReplyModes cousins={data.cousins} modes={data.modes} installDefault={data.config.reply_default} onSaved={load} />
  </div>;
}

function LanguageGlossary() {
  const [path, setPath] = React.useState("");
  const [top, setTop] = React.useState(50);
  const [glossary, setGlossary] = React.useState("");
  const [groupsText, setGroupsText] = React.useState("");
  const [terms, setTerms] = React.useState(null);
  const [groups, setGroups] = React.useState(null);
  const [derived, setDerived] = React.useState(null);
  const [busy, setBusy] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const body = () => {
    const b = { path: path.trim(), groups: languageGroupsParse(groupsText) };
    if (glossary.trim()) b.glossary = glossary.trim();
    return b;
  };
  const act = async (which, fn) => {
    setBusy(which); setErr(null);
    try { await fn(); } catch (e) { setErr(String(e.message || e)); } finally { setBusy(null); }
  };
  const findTerms = () => act("terms", async () => {
    const q = new URLSearchParams({ path: path.trim(), top: String(top) });
    if (glossary.trim()) q.set("glossary", glossary.trim());
    setTerms(await langSend("GET", `/api/language/terms?${q}`));
  });
  const checkGroups = () => act("groups", async () => {
    setGroups(await langSend("POST", "/api/language/groups", body()));
  });
  const derive = () => act("derive", async () => {
    setDerived(await langSend("POST", "/api/language/derive", body()));
  });
  const mark = { term: "in the glossary", avoid: "a do-not-use word" };
  return <div style={{ display: "flex", flexDirection: "column", gap: 8 }} data-language-glossary>
    <div className="muted">
      The words a repository uses, in its code (identifiers split at snake_case, camelCase and
      kebab-case) and its docs (comments, strings, Markdown). A path in the install, a cousin's
      home or the framework checkout.
    </div>
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <input className="txt" value={path} onChange={e => setPath(e.target.value)}
             placeholder="path (relative to the install root, or absolute)" style={{ ...LANG_MONO, flex: 2, minWidth: 240 }} />
      <input className="txt" value={glossary} onChange={e => setGlossary(e.target.value)}
             placeholder="glossary file (optional; else the configured one)" style={{ ...LANG_MONO, flex: 1, minWidth: 200 }} />
    </div>
    <textarea className="txt" rows={3} value={groupsText} onChange={e => setGroupsText(e.target.value)}
              placeholder="more synonym groups, one per line: preferred, other, other" style={{ ...LANG_MONO, width: "100%" }} />
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <span className="muted">top</span>
      <input className="txt" type="number" min="1" max="1000" value={top} style={{ width: 80 }}
             onChange={e => setTop(Number(e.target.value) || 50)} />
      <button className="btn primary" disabled={!!busy || !path.trim()} onClick={findTerms}>{busy === "terms" ? "Finding..." : "Find terms"}</button>
      <button className="btn" disabled={!!busy || !path.trim()} onClick={checkGroups}>{busy === "groups" ? "Checking..." : "Check groups"}</button>
      <button className="btn" disabled={!!busy || !path.trim()} onClick={derive}
              title="a glossary whose terms are the words the code uses, to copy">{busy === "derive" ? "Deriving..." : "Derive"}</button>
    </div>
    {err && <div style={{ color: "var(--red)" }}>{err}</div>}
    {groups && <>
      <SectionLabel>synonym groups ({groups.flagged} flagged)</SectionLabel>
      <table className="data" data-language-groups>
        <thead><tr><th>entry</th><th>uses (code / docs)</th><th>dominant</th><th></th></tr></thead>
        <tbody>
          {groups.groups.map((g, i) => <tr key={i}>
            <td>{g.term}</td>
            <td style={LANG_MONO}>{g.members.map(m => `${m.word} ${m.code}/${m.docs}`).join(", ")}</td>
            <td>{g.dominant}</td>
            <td>{g.flagged ? <span className="pill amber" title={g.message}>{g.message}</span> : <span className="pill green">ok</span>}</td>
          </tr>)}
        </tbody>
      </table>
    </>}
    {derived && <>
      <SectionLabel>derived glossary ({derived.entries} entries): copy it into a file</SectionLabel>
      <pre style={LANG_PRE} data-language-derived>{derived.text}</pre>
    </>}
    {terms && <>
      <SectionLabel>terms ({terms.files} files read, {terms.skipped} skipped{terms.truncated ? ", stopped at the file limit" : ""})</SectionLabel>
      <table className="data" data-language-terms>
        <thead><tr><th>term</th><th>code</th><th>docs</th><th>glossary</th></tr></thead>
        <tbody>
          {terms.terms.map(t => <tr key={t.term}>
            <td style={LANG_MONO}>{t.term}</td><td>{t.code}</td><td>{t.docs}</td>
            <td className="muted">{mark[t.glossary] || ""}</td>
          </tr>)}
        </tbody>
      </table>
    </>}
  </div>;
}

function LanguageView() {
  const [tab, setTab] = React.useState("check");
  return <div className="wrap-pad" style={{ maxWidth: 1100 }} data-language-view>
    <div className="radio-row">
      {LANG_TABS.map(([id, label]) => <button key={id} className={tab === id ? "sel" : ""} onClick={() => setTab(id)}>{label}</button>)}
    </div>
    {tab === "check" && <LanguageCheck />}
    {tab === "glossary" && <LanguageGlossary />}
    {tab === "settings" && <LanguageSettings />}
  </div>;
}

registerView({ id: "language", label: "Language", icon: I.logs, order: 70, render: (props) => <LanguageView {...props} /> });

Object.assign(window, { LanguageView, languageBannedText, languageBannedParse, languageGroupsParse });
