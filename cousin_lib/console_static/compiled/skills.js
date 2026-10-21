const SKILL_MONO = { fontFamily: "var(--mono)", fontSize: 12 };
const SKILL_TONES = { "read-only": "green", "writes-local": "amber", "outward": "red" };
async function skillPost(path, body) {
  const { r, d } = await apiSend("POST", path, body || {});
  if (!r.ok) throw new Error(d && d.error || `HTTP ${r.status}`);
  return d;
}
function skillPath(...parts) {
  return parts.map((p) => encodeURIComponent(p)).join("/");
}
function SkillSideEffect({ value }) {
  return /* @__PURE__ */ React.createElement(
    "span",
    {
      className: `pill ${SKILL_TONES[value] || "gray"}`,
      title: "what the skill can change; an outward step stops at the operator's go"
    },
    value || "side-effect?"
  );
}
function SkillDiff({ text }) {
  if (!text) return /* @__PURE__ */ React.createElement("div", { className: "muted", style: SKILL_MONO }, "no difference from what is approved");
  return /* @__PURE__ */ React.createElement("pre", { style: {
    ...SKILL_MONO,
    whiteSpace: "pre-wrap",
    maxHeight: 420,
    overflow: "auto",
    background: "var(--bg-0)",
    padding: 10,
    borderRadius: 6,
    margin: 0
  } }, text.split("\n").map((line, i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { color: line.startsWith("+") && !line.startsWith("+++") ? "var(--green)" : line.startsWith("-") && !line.startsWith("---") ? "var(--red)" : "var(--fg-2)" } }, line || " ")));
}
function SkillMeta({ metadata }) {
  const keys = ["scope", "context", "hosts", "reads", "rests-on", "should-trigger", "must-not-trigger", "source"];
  const rows = keys.filter((k) => metadata && metadata[k]);
  if (!rows.length) return null;
  return /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px", ...SKILL_MONO } }, rows.map((k) => /* @__PURE__ */ React.createElement(React.Fragment, { key: k }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, k), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-1)" } }, metadata[k]))));
}
function SkillTrigger({ result }) {
  if (!result) return /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "not run on this version");
  const line = (key, label) => {
    const r = result[key] || {};
    return /* @__PURE__ */ React.createElement("div", { key, style: SKILL_MONO }, /* @__PURE__ */ React.createElement("span", { className: `pill ${r.pass ? "green" : "red"}` }, r.pass ? "pass" : "fail"), /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 8 } }, label, ': "', r.prompt, '"'), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { marginLeft: 8 } }, "fired ", r.fired || "nothing"));
  };
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, line("should-trigger", "should fire"), line("must-not-trigger", "must not fire"), /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "on ", result.model, ", ", result.at));
}
function SkillDraftReview({ slug, name, onDone }) {
  const [d, setD] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const base = `/api/cousins/${skillPath(slug)}/skills/drafts/${skillPath(name)}`;
  React.useEffect(() => {
    setD(null);
    setErr(null);
    apiGet(base).then((x) => x ? setD(x) : setErr("this draft cannot be read"));
  }, [base]);
  const act = async (fn) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  if (err && !d) return /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: err } });
  if (!d) return /* @__PURE__ */ React.createElement("div", { className: "muted", style: SKILL_MONO }, "loading...");
  const blocked = (d.problems || []).length > 0;
  const untested = !d.trigger;
  const missed = !!d.trigger && !d.trigger.pass;
  return /* @__PURE__ */ React.createElement("div", { style: {
    display: "flex",
    flexDirection: "column",
    gap: 10,
    paddingLeft: 12,
    borderLeft: "2px solid var(--hair)"
  } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", ...SKILL_MONO } }, /* @__PURE__ */ React.createElement(SkillSideEffect, { value: (d.metadata || {})["side-effect"] }), /* @__PURE__ */ React.createElement("span", null, "goes to ", d.repo || "?", ":", d.folder || "?", "/", d.name), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "files: ", (d.files || []).join(", "))), blocked && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: d.problems.join("; ") } }), /* @__PURE__ */ React.createElement(SkillMeta, { metadata: d.metadata }), /* @__PURE__ */ React.createElement(SkillDiff, { text: d.diff }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "flex-start" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      disabled: busy,
      onClick: () => act(async () => {
        const res = await skillPost(`${base}/trigger-test`);
        setD((x) => ({ ...x, trigger: res.trigger }));
      })
    },
    busy ? "running..." : "run the trigger test"
  ), /* @__PURE__ */ React.createElement(SkillTrigger, { result: d.trigger })), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    SysConfirmButton,
    {
      label: missed ? "approve anyway" : "approve",
      confirmLabel: missed ? "click again: approve despite the failed trigger test" : "click again: commit it and load it at the next start",
      tone: "primary",
      small: false,
      disabled: busy || blocked || !d.diff || untested,
      onConfirm: () => act(async () => {
        const res = await skillPost(`${base}/approve`, { diff_sha256: d.diff_sha256 });
        onDone({ ok: true, text: `approved ${d.name}: ${res.note}` });
      })
    }
  ), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: reason,
      onChange: (e) => setReason(e.target.value),
      placeholder: "why it is rejected (optional)",
      style: { flex: 1, minWidth: 180 }
    }
  ), /* @__PURE__ */ React.createElement(
    SysConfirmButton,
    {
      label: "reject",
      small: false,
      disabled: busy,
      onConfirm: () => act(async () => {
        await skillPost(`${base}/reject`, { reason });
        onDone({ ok: true, text: `rejected ${d.name}` });
      })
    }
  )), err && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: err } }));
}
function SkillApproved({ showUrl, removalUrl, removeUrl, onDone }) {
  const [s, setS] = React.useState(null);
  const [removal, setRemoval] = React.useState(null);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    setS(null);
    setRemoval(null);
    setErr(null);
    apiGet(showUrl).then((x) => x ? setS(x) : setErr("this skill cannot be read"));
  }, [showUrl]);
  if (err) return /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: err } });
  if (!s) return /* @__PURE__ */ React.createElement("div", { className: "muted", style: SKILL_MONO }, "loading...");
  return /* @__PURE__ */ React.createElement("div", { style: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    paddingLeft: 12,
    borderLeft: "2px solid var(--hair)"
  } }, /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "files: ", (s.files || []).join(", ")), /* @__PURE__ */ React.createElement("pre", { style: {
    ...SKILL_MONO,
    whiteSpace: "pre-wrap",
    maxHeight: 420,
    overflow: "auto",
    background: "var(--bg-0)",
    padding: 10,
    borderRadius: 6,
    margin: 0
  } }, s.text), !removal && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: { alignSelf: "flex-start" },
      onClick: () => apiGet(removalUrl).then((x) => x ? setRemoval(x) : setErr("no removal diff"))
    },
    "remove..."
  ), removal && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SkillDiff, { text: removal.diff }), /* @__PURE__ */ React.createElement(
    SysConfirmButton,
    {
      label: "remove it",
      confirmLabel: "click again: commit the removal",
      small: false,
      onConfirm: async () => {
        try {
          const res = await skillPost(removeUrl, { diff_sha256: removal.diff_sha256 });
          onDone({ ok: true, text: `removed: ${res.note}` });
        } catch (e) {
          setErr(String(e.message || e));
        }
      }
    }
  )));
}
function SkillRow({ label, item, open, onToggle, children }) {
  const meta = item.metadata || {};
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement("div", { onClick: onToggle, style: { display: "flex", gap: 10, alignItems: "baseline", cursor: "pointer", ...SKILL_MONO } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)", minWidth: 180 } }, open ? "v" : ">", " ", label), /* @__PURE__ */ React.createElement(SkillSideEffect, { value: meta["side-effect"] }), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { flex: 1 } }, item.description), (item.problems || []).length > 0 && /* @__PURE__ */ React.createElement("span", { className: "pill red", title: item.problems.join("; ") }, "problem")), open && children);
}
function SkillsSystemView() {
  const [data, setData] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const load = () => apiGet("/api/skills").then((d) => setData(d || { repos: [], drafts: [] }));
  React.useEffect(() => {
    load();
  }, []);
  const done = (msg) => {
    setNote(msg);
    setOpen(null);
    load();
  };
  const toggle = (key) => setOpen((o) => o === key ? null : key);
  if (!data) return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad muted" }, "loading skills...");
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", style: { maxWidth: 1100 } }, note && /* @__PURE__ */ React.createElement(SysNote, { msg: note }), /* @__PURE__ */ React.createElement(SysPanel, { title: "drafts waiting", sub: `${data.drafts.length} from the cousins; a private cousin's are in its inspector` }, data.drafts.length === 0 && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "none"), data.drafts.map((d) => {
    const key = `draft/${d.cousin}/${d.name}`;
    return /* @__PURE__ */ React.createElement(
      SkillRow,
      {
        key,
        label: `${d.cousin}: ${d.name}`,
        item: d,
        open: open === key,
        onToggle: () => toggle(key)
      },
      /* @__PURE__ */ React.createElement(SkillDraftReview, { slug: d.cousin, name: d.name, onDone: done })
    );
  })), data.repos.map((repo) => /* @__PURE__ */ React.createElement(
    SysPanel,
    {
      key: repo.repo,
      title: repo.repo,
      sub: !repo.exists ? "no repository yet: the first approval creates it" : repo.trusted ? `approved at ${String(repo.approved).slice(0, 10)}` : "main has moved past its last approval: nothing loads from it"
    },
    repo.exists && repo.folders.length === 0 && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "no skills"),
    repo.folders.map((f) => /* @__PURE__ */ React.createElement("div", { key: f.folder, style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, f.folder === "shared" ? "shared (every cousin)" : f.folder), f.skills.map((sk) => {
      const key = `${repo.repo}/${f.folder}/${sk.name}`;
      const urls = f.folder === "shared" ? { base: `/api/skills/shared/${skillPath(sk.name)}` } : { base: `/api/cousins/${skillPath(f.folder)}/skills/approved/${skillPath(repo.repo, sk.name)}` };
      return /* @__PURE__ */ React.createElement(SkillRow, { key, label: sk.name, item: sk, open: open === key, onToggle: () => toggle(key) }, /* @__PURE__ */ React.createElement(
        SkillApproved,
        {
          showUrl: urls.base,
          removalUrl: `${urls.base}/removal`,
          removeUrl: `${urls.base}/remove`,
          onDone: done
        }
      ));
    })))
  )));
}
function CousinSkillsPanel({ cousin }) {
  const c = cousin;
  const [data, setData] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const base = `/api/cousins/${skillPath(c.slug)}/skills`;
  const load = () => apiGet(base).then((d) => setData(d));
  React.useEffect(() => {
    setData(null);
    setNote(null);
    setOpen(null);
    if (!c.remote) load();
  }, [c.slug]);
  if (c.remote || !data) return null;
  const done = (msg) => {
    setNote(msg);
    setOpen(null);
    load();
  };
  const toggle = (key) => setOpen((o) => o === key ? null : key);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "skills"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 } }, note && /* @__PURE__ */ React.createElement(SysNote, { msg: note }), data.error && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: data.error } }), data.lane === "opencode" && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "this lane loads no skills"), /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "repositories: ", (data.repos || []).join(", ") || "none", "; loads at the next start"), (data.loaded || []).map((sk) => {
    const key = `loaded/${sk.plugin}/${sk.name}`;
    const url = `${base}/approved/${skillPath(sk.plugin, sk.name)}`;
    return /* @__PURE__ */ React.createElement(
      SkillRow,
      {
        key,
        label: `${sk.plugin}:${sk.name}`,
        item: sk,
        open: open === key,
        onToggle: () => toggle(key)
      },
      /* @__PURE__ */ React.createElement(SkillApproved, { showUrl: url, removalUrl: `${url}/removal`, removeUrl: `${url}/remove`, onDone: done })
    );
  }), (data.loaded || []).length === 0 && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "no approved skills"), (data.drafts || []).map((d) => {
    const key = `draft/${d.name}`;
    return /* @__PURE__ */ React.createElement(
      SkillRow,
      {
        key,
        label: `draft: ${d.name}`,
        item: d,
        open: open === key,
        onToggle: () => toggle(key)
      },
      /* @__PURE__ */ React.createElement(SkillDraftReview, { slug: c.slug, name: d.name, onDone: done })
    );
  })));
}
registerSlot("system.tabs", {
  id: "skills",
  label: "skills",
  order: 45,
  render: () => /* @__PURE__ */ React.createElement(SkillsSystemView, null)
});
registerSlot("inspector.panels", {
  id: "skills",
  order: 35,
  render: ({ cousin }) => /* @__PURE__ */ React.createElement(CousinSkillsPanel, { cousin })
});
Object.assign(window, { SkillsSystemView, CousinSkillsPanel, SkillDraftReview, SkillApproved });
