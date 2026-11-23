function languageBannedText(list) {
  return (list || []).map((b) => b.use ? `${b.word} = ${b.use}` : b.word).join("\n");
}
function languageBannedParse(text) {
  return String(text || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const at = l.indexOf("=");
    if (at < 0) return { word: l, use: "" };
    return { word: l.slice(0, at).trim(), use: l.slice(at + 1).trim() };
  }).filter((b) => b.word);
}
function languageGroupsParse(text) {
  return String(text || "").split("\n").map((l) => l.split(",").map((w) => w.trim()).filter(Boolean).join(",")).filter(Boolean);
}
const LANG_MONO = { fontFamily: "var(--mono)", fontSize: 12 };
const LANG_PRE = {
  ...LANG_MONO,
  whiteSpace: "pre-wrap",
  maxHeight: 420,
  overflow: "auto",
  background: "var(--bg-0)",
  padding: 10,
  borderRadius: 6,
  margin: 0
};
const LANG_TABS = [["check", "check"], ["glossary", "glossary"], ["settings", "settings"]];
async function langSend(method, path, body) {
  const { r, d } = await apiSend(method, path, body);
  if (!r.ok) throw new Error(d && d.error || `HTTP ${r.status}`);
  return d;
}
function LanguageFindings({ findings }) {
  if (!findings) return null;
  if (!findings.length) return /* @__PURE__ */ React.createElement("div", { className: "muted", style: LANG_MONO }, "no findings");
  return /* @__PURE__ */ React.createElement("table", { className: "data", "data-language-findings": true }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "line"), /* @__PURE__ */ React.createElement("th", null, "rule"), /* @__PURE__ */ React.createElement("th", null, "message"), /* @__PURE__ */ React.createElement("th", null, "text"))), /* @__PURE__ */ React.createElement("tbody", null, findings.map((f, i) => /* @__PURE__ */ React.createElement("tr", { key: i }, /* @__PURE__ */ React.createElement("td", { style: LANG_MONO }, f.line, ":", f.col), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: `pill ${f.severity === "note" ? "gray" : "amber"}` }, f.rule)), /* @__PURE__ */ React.createElement("td", null, f.message), /* @__PURE__ */ React.createElement("td", { style: LANG_MONO }, f.excerpt)))));
}
function LanguageCheck() {
  const [text, setText] = React.useState("");
  const [glossary, setGlossary] = React.useState("");
  const [result, setResult] = React.useState(null);
  const [showFixed, setShowFixed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const run = async () => {
    setBusy(true);
    setErr(null);
    setShowFixed(false);
    try {
      const body = { text };
      if (glossary.trim()) body.glossary = glossary.trim();
      setResult(await langSend("POST", "/api/language/check", body));
    } catch (e) {
      setErr(String(e.message || e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  };
  const fixable = result ? result.findings.filter((f) => f.fix != null).length : 0;
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt",
      rows: 12,
      value: text,
      onChange: (e) => setText(e.target.value),
      placeholder: "paste the text or Markdown to check",
      style: { ...LANG_MONO, width: "100%" }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: glossary,
      onChange: (e) => setGlossary(e.target.value),
      placeholder: "glossary file (optional; else the configured one)",
      style: { ...LANG_MONO, flex: 1, minWidth: 240 }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: busy || !text.trim(), onClick: run }, "Check"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      disabled: !result || !fixable,
      onClick: () => setShowFixed((s) => !s),
      title: "the text with each fixable finding (an em dash) fixed"
    },
    showFixed ? "Hide fix" : "Fix"
  )), err && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)" } }, err), result && /* @__PURE__ */ React.createElement("div", { className: "muted", style: LANG_MONO }, result.findings.length, " finding", result.findings.length === 1 ? "" : "s", ", ", fixable, " fixable, glossary of ", result.glossary_terms, " term", result.glossary_terms === 1 ? "" : "s"), showFixed && result && /* @__PURE__ */ React.createElement("pre", { style: LANG_PRE, "data-language-fixed": true }, result.fixed), result && /* @__PURE__ */ React.createElement(LanguageFindings, { findings: result.findings }));
}
function LanguageReplyModes({ cousins, modes, installDefault, onSaved }) {
  const [busy, setBusy] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const set = async (slug, value) => {
    setBusy(slug);
    setErr(null);
    try {
      await langSend(
        "POST",
        `/api/cousins/${encodeURIComponent(slug)}/settings`,
        { changes: { "plain_language.reply": value === "" ? null : value } }
      );
      onSaved();
    } catch (e) {
      setErr(`${slug}: ${String(e.message || e)}`);
    } finally {
      setBusy(null);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { "data-language-reply": true }, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "reply mode per cousin"), /* @__PURE__ */ React.createElement("div", { className: "muted", style: { marginBottom: 6 } }, "What a cousin's reply and send do with the check: off; note (the text goes as written, the findings come back to the cousin); fix (em dashes fixed before it goes, the rest noted). It never blocks a reply. Applies from the next reply, without a restart."), err && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)" } }, err), /* @__PURE__ */ React.createElement("table", { className: "data" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "cousin"), /* @__PURE__ */ React.createElement("th", null, "reply"))), /* @__PURE__ */ React.createElement("tbody", null, cousins.map((c) => /* @__PURE__ */ React.createElement("tr", { key: c.slug }, /* @__PURE__ */ React.createElement("td", null, c.name, " ", /* @__PURE__ */ React.createElement("span", { className: "muted", style: LANG_MONO }, c.slug)), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement(
    "select",
    {
      className: "sel",
      value: c.set ? c.reply : "",
      disabled: busy === c.slug,
      onChange: (e) => set(c.slug, e.target.value)
    },
    /* @__PURE__ */ React.createElement("option", { value: "" }, "install default (", installDefault, ")"),
    modes.map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m }, m))
  )))))));
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
    if (!d) {
      setErr("could not read the config");
      return;
    }
    setData(d);
    setDraft(d.config);
    setBanned(languageBannedText(d.config.banned));
  };
  React.useEffect(() => {
    load();
  }, []);
  if (!data || !draft) return /* @__PURE__ */ React.createElement("div", { className: "muted" }, err || "loading...");
  const put = (k, v) => setDraft(Object.assign({}, draft, { [k]: v }));
  const save = async () => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const d = await langSend(
        "PUT",
        "/api/language/config",
        { config: Object.assign({}, draft, { banned: languageBannedParse(banned) }) }
      );
      setDraft(d.config);
      setBanned(languageBannedText(d.config.banned));
      setMsg(`saved to ${data.path}`);
      load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 }, "data-language-settings": true }, /* @__PURE__ */ React.createElement(SectionLabel, null, data.path, data.exists ? "" : " (not written yet: the defaults)"), /* @__PURE__ */ React.createElement("div", { className: "muted" }, "Save writes the whole file from these values: comments in it are lost."), data.error && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)" } }, "the file does not check: ", data.error, "; saving replaces it"), /* @__PURE__ */ React.createElement("dl", { className: "kv", style: { gridTemplateColumns: "160px 1fr" } }, ["step", "description"].map((k) => /* @__PURE__ */ React.createElement(React.Fragment, { key: k }, /* @__PURE__ */ React.createElement("dt", null, k, " limit"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      type: "number",
      min: "1",
      max: "1000",
      value: draft.limits[k],
      disabled: busy,
      style: { width: 90 },
      onChange: (e) => put("limits", Object.assign({}, draft.limits, { [k]: Number(e.target.value) }))
    }
  ), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { marginLeft: 8 } }, k === "step" ? "words per sentence in a numbered item or one that starts with a verb" : "words per sentence anywhere else")))), /* @__PURE__ */ React.createElement("dt", null, "rules"), /* @__PURE__ */ React.createElement("dd", { style: { display: "flex", gap: 12, flexWrap: "wrap" } }, Object.keys(draft.rules).map((r) => /* @__PURE__ */ React.createElement("label", { key: r, style: { display: "inline-flex", gap: 4, alignItems: "center" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "checkbox",
      checked: draft.rules[r],
      disabled: busy,
      onChange: (e) => put("rules", Object.assign({}, draft.rules, { [r]: e.target.checked }))
    }
  ), /* @__PURE__ */ React.createElement("span", { style: LANG_MONO }, r)))), /* @__PURE__ */ React.createElement("dt", null, "banned words"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt",
      rows: 5,
      value: banned,
      disabled: busy,
      onChange: (e) => setBanned(e.target.value),
      placeholder: "leverage = use",
      style: { ...LANG_MONO, width: "100%" }
    }
  ), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "one per line: the word or phrase, then = and the word to use instead")), /* @__PURE__ */ React.createElement("dt", null, "glossary"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft.glossary,
      disabled: busy,
      onChange: (e) => put("glossary", e.target.value),
      placeholder: "a path, relative to the install root",
      style: { ...LANG_MONO, width: "100%" }
    }
  )), /* @__PURE__ */ React.createElement("dt", null, "reply default"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement("select", { className: "sel", value: draft.reply_default, disabled: busy, onChange: (e) => put("reply_default", e.target.value) }, data.modes.map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m }, m))), " ", /* @__PURE__ */ React.createElement("span", { className: "muted" }, "for a cousin whose cousin.toml sets no reply mode"))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center" } }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: busy, onClick: save }, "Save"), msg && /* @__PURE__ */ React.createElement("span", { className: "muted" }, msg), err && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red)" } }, err)), /* @__PURE__ */ React.createElement(LanguageReplyModes, { cousins: data.cousins, modes: data.modes, installDefault: data.config.reply_default, onSaved: load }));
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
    setBusy(which);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(null);
    }
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
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 }, "data-language-glossary": true }, /* @__PURE__ */ React.createElement("div", { className: "muted" }, "The words a repository uses, in its code (identifiers split at snake_case, camelCase and kebab-case) and its docs (comments, strings, Markdown). A path in the install, a cousin's home or the framework checkout."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: path,
      onChange: (e) => setPath(e.target.value),
      placeholder: "path (relative to the install root, or absolute)",
      style: { ...LANG_MONO, flex: 2, minWidth: 240 }
    }
  ), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: glossary,
      onChange: (e) => setGlossary(e.target.value),
      placeholder: "glossary file (optional; else the configured one)",
      style: { ...LANG_MONO, flex: 1, minWidth: 200 }
    }
  )), /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt",
      rows: 3,
      value: groupsText,
      onChange: (e) => setGroupsText(e.target.value),
      placeholder: "more synonym groups, one per line: preferred, other, other",
      style: { ...LANG_MONO, width: "100%" }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, "top"), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      type: "number",
      min: "1",
      max: "1000",
      value: top,
      style: { width: 80 },
      onChange: (e) => setTop(Number(e.target.value) || 50)
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !!busy || !path.trim(), onClick: findTerms }, busy === "terms" ? "Finding..." : "Find terms"), /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: !!busy || !path.trim(), onClick: checkGroups }, busy === "groups" ? "Checking..." : "Check groups"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      disabled: !!busy || !path.trim(),
      onClick: derive,
      title: "a glossary whose terms are the words the code uses, to copy"
    },
    busy === "derive" ? "Deriving..." : "Derive"
  )), err && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)" } }, err), groups && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, null, "synonym groups (", groups.flagged, " flagged)"), /* @__PURE__ */ React.createElement("table", { className: "data", "data-language-groups": true }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "entry"), /* @__PURE__ */ React.createElement("th", null, "uses (code / docs)"), /* @__PURE__ */ React.createElement("th", null, "dominant"), /* @__PURE__ */ React.createElement("th", null))), /* @__PURE__ */ React.createElement("tbody", null, groups.groups.map((g, i) => /* @__PURE__ */ React.createElement("tr", { key: i }, /* @__PURE__ */ React.createElement("td", null, g.term), /* @__PURE__ */ React.createElement("td", { style: LANG_MONO }, g.members.map((m) => `${m.word} ${m.code}/${m.docs}`).join(", ")), /* @__PURE__ */ React.createElement("td", null, g.dominant), /* @__PURE__ */ React.createElement("td", null, g.flagged ? /* @__PURE__ */ React.createElement("span", { className: "pill amber", title: g.message }, g.message) : /* @__PURE__ */ React.createElement("span", { className: "pill green" }, "ok"))))))), derived && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, null, "derived glossary (", derived.entries, " entries): copy it into a file"), /* @__PURE__ */ React.createElement("pre", { style: LANG_PRE, "data-language-derived": true }, derived.text)), terms && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, null, "terms (", terms.files, " files read, ", terms.skipped, " skipped", terms.truncated ? ", stopped at the file limit" : "", ")"), /* @__PURE__ */ React.createElement("table", { className: "data", "data-language-terms": true }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "term"), /* @__PURE__ */ React.createElement("th", null, "code"), /* @__PURE__ */ React.createElement("th", null, "docs"), /* @__PURE__ */ React.createElement("th", null, "glossary"))), /* @__PURE__ */ React.createElement("tbody", null, terms.terms.map((t) => /* @__PURE__ */ React.createElement("tr", { key: t.term }, /* @__PURE__ */ React.createElement("td", { style: LANG_MONO }, t.term), /* @__PURE__ */ React.createElement("td", null, t.code), /* @__PURE__ */ React.createElement("td", null, t.docs), /* @__PURE__ */ React.createElement("td", { className: "muted" }, mark[t.glossary] || "")))))));
}
function LanguageView() {
  const [tab, setTab] = React.useState("check");
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", style: { maxWidth: 1100 }, "data-language-view": true }, /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, LANG_TABS.map(([id, label]) => /* @__PURE__ */ React.createElement("button", { key: id, className: tab === id ? "sel" : "", onClick: () => setTab(id) }, label))), tab === "check" && /* @__PURE__ */ React.createElement(LanguageCheck, null), tab === "glossary" && /* @__PURE__ */ React.createElement(LanguageGlossary, null), tab === "settings" && /* @__PURE__ */ React.createElement(LanguageSettings, null));
}
registerView({ id: "language", label: "Language", icon: I.logs, order: 70, render: (props) => /* @__PURE__ */ React.createElement(LanguageView, { ...props }) });
Object.assign(window, { LanguageView, languageBannedText, languageBannedParse, languageGroupsParse });
