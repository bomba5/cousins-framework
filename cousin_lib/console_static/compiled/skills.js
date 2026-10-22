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
  const m = metadata || {};
  const keys = ["scope", "context", "hosts", "reads", "rests-on", "should-trigger", "must-not-trigger", "source"];
  const always = ["hosts", "reads"];
  const has = (k) => Object.prototype.hasOwnProperty.call(m, k);
  const rows = keys.filter((k) => always.includes(k) || has(k) && m[k] !== "");
  return /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px", ...SKILL_MONO } }, rows.map((k) => /* @__PURE__ */ React.createElement(React.Fragment, { key: k }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, k), !has(k) ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "not declared") : m[k] === "" ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "none") : /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-1)", overflowWrap: "anywhere" } }, m[k]))));
}
function SkillTrigger({ result }) {
  if (!result) return /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "not run on this version");
  const line = (key, label) => {
    const r = result[key] || {};
    return /* @__PURE__ */ React.createElement("div", { key, style: SKILL_MONO }, /* @__PURE__ */ React.createElement("span", { className: `pill ${r.pass ? "green" : "red"}` }, r.pass ? "pass" : "fail"), /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 8 } }, label, ': "', r.prompt, '"'), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { marginLeft: 8 } }, "fired ", r.fired || "nothing"));
  };
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, line("should-trigger", "should fire"), line("must-not-trigger", "must not fire"), /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "on ", result.model, ", ", result.at));
}
function SkillOutside({ d, base, onAllowed }) {
  const o = d.outside;
  const [up, setUp] = React.useState(null);
  const [showLicence, setShowLicence] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    setUp(null);
    apiSend("GET", `${base}/upstream`).then(({ r, d: x }) => setUp(r.ok ? x : { error: x && x.error || `HTTP ${r.status}` }));
  }, [base, d.diff_sha256]);
  const run = o.dry_run;
  const scripts = o.scripts || [];
  const row = (label, value) => /* @__PURE__ */ React.createElement("div", { style: { display: "contents" } }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, label), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-1)", overflowWrap: "anywhere" } }, value));
  return /* @__PURE__ */ React.createElement("div", { "data-skill-outside": true, style: { display: "flex", flexDirection: "column", gap: 6, ...SKILL_MONO } }, /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px" } }, row("from outside", `${o.url || "?"} @ ${(o.commit || "?").slice(0, 12)} : ${o.folder || "?"}`), row("licence", /* @__PURE__ */ React.createElement("span", null, o.license || "none", " (", o.license_file || "no licence file", ")", " ", o.license_text && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: { fontSize: 10, padding: "0 6px" },
      onClick: () => setShowLicence((s) => !s)
    },
    showLicence ? "hide" : "read it"
  ))), row("can run", scripts.length ? scripts.map((s) => `${s.path} (${s.why})`).join("; ") : "nothing"), row("source now", !up ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "fetching it again...") : up.error ? /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red)" } }, up.error) : up.differ.length ? /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red)" } }, "not as published: ", up.differ.join(", ")) : /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("span", { className: "pill green" }, "as published"), " past the frontmatter the import writes")), up && !up.error && up.not_kept.length > 0 && row("not kept", up.not_kept.map(
    (k) => (up.upstream || {})[k] ? `${k} (${up.upstream[k]})` : k
  ).join(", ")), row("dry run", /* @__PURE__ */ React.createElement("span", null, 'task: "', o.task || "none", '"', " ", !run && /* @__PURE__ */ React.createElement(
    SysConfirmButton,
    {
      label: "allow a dry run",
      tone: "primary",
      disabled: busy || !o.task,
      confirmLabel: "click again: I read its scripts; the cousin may run the task once",
      onConfirm: async () => {
        setBusy(true);
        setErr(null);
        try {
          const res = await skillPost(`${base}/dry-run/allow`, { diff_sha256: d.diff_sha256 });
          onAllowed(res.dry_run, res.told);
        } catch (e) {
          setErr(String(e.message || e));
        } finally {
          setBusy(false);
        }
      }
    }
  ), run && run.job == null && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "allowed by ", run.allowed_by, " at ", run.allowed_at, "; waiting for the cousin's job"), run && run.job != null && /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("span", { className: `pill ${run.pass ? "green" : "red"}` }, run.pass ? "pass" : "fail"), " ", "job #", run.job, " ", run.status, ", exit ", String(run.exit_code), run.log ? ` · log ${run.log}` : "")))), showLicence && /* @__PURE__ */ React.createElement("pre", { style: {
    ...SKILL_MONO,
    whiteSpace: "pre-wrap",
    maxHeight: 300,
    overflow: "auto",
    background: "var(--bg-0)",
    padding: 10,
    borderRadius: 6,
    margin: 0
  } }, o.license_text), err && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: err } }));
}
function SkillDraftReview({ slug, name, onDone }) {
  const [d, setD] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [told, setTold] = React.useState(null);
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
  const run = d.outside ? d.outside.dry_run : null;
  const unrun = !!d.outside && !(run && run.job != null);
  const missed = !!d.trigger && !d.trigger.pass || !!run && run.job != null && !run.pass;
  return /* @__PURE__ */ React.createElement("div", { style: {
    display: "flex",
    flexDirection: "column",
    gap: 10,
    paddingLeft: 12,
    borderLeft: "2px solid var(--hair)"
  } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", ...SKILL_MONO } }, /* @__PURE__ */ React.createElement(SkillSideEffect, { value: (d.metadata || {})["side-effect"] }), /* @__PURE__ */ React.createElement("span", null, "goes to ", d.repo || "?", ":", d.folder || "?", "/", d.name), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { overflowWrap: "anywhere" } }, "files: ", (d.files || []).join(", "))), blocked && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: d.problems.join("; ") } }), /* @__PURE__ */ React.createElement(SkillMeta, { metadata: d.metadata }), d.outside && /* @__PURE__ */ React.createElement(SkillOutside, { d, base, onAllowed: (entry, ok) => {
    setD((x) => ({ ...x, outside: { ...x.outside, dry_run: entry } }));
    setTold(ok ? `${slug} was told to run it` : `${slug} could not be told: ask it in the chat`);
  } }), told && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, told), /* @__PURE__ */ React.createElement(SkillDiff, { text: d.diff }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "flex-start" } }, /* @__PURE__ */ React.createElement(
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
      confirmLabel: missed ? "click again: approve despite a failed check" : "click again: commit it and load it at the next start",
      tone: "primary",
      small: false,
      disabled: busy || blocked || !d.diff || untested || unrun,
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
function skillWhen(iso) {
  return iso ? String(iso).replace("T", " ").slice(0, 16) : "never";
}
function SkillHealth({ item, unusedDays }) {
  const check = item.check;
  const use = item.usage || {};
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap", paddingLeft: 18, ...SKILL_MONO } }, !check && /* @__PURE__ */ React.createElement("span", { className: "pill gray", title: "the staleness run has not reached it yet" }, "not checked yet"), check && check.stale && /* @__PURE__ */ React.createElement("span", { className: "pill red", title: `changed: ${(check.changed || []).join(", ")}` }, "stale"), check && !check.stale && /* @__PURE__ */ React.createElement("span", { className: "pill green", title: `checked ${skillWhen(check.at)}` }, "ok"), check && check.update && /* @__PURE__ */ React.createElement("span", { className: "pill amber", title: "its source upstream changed: a new import, by the operator" }, "upstream changed"), item.unused && /* @__PURE__ */ React.createElement("span", { className: "pill gray", title: `no use for ${unusedDays || 30} days: consider removing it` }, "unused ", unusedDays || 30, "d"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "used ", skillWhen(use.last), use.count ? ` (${use.count}x)` : ""), check && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "checked ", skillWhen(check.at), check.unknown ? `, ${check.unknown} not checkable` : ""), check && check.stale && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red)" } }, "changed: ", (check.changed || []).join(", ")));
}
function SkillRow({ label, item, open, onToggle, children, unusedDays }) {
  const meta = item.metadata || {};
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement("div", { onClick: onToggle, style: { display: "flex", gap: 10, alignItems: "baseline", cursor: "pointer", ...SKILL_MONO } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)", minWidth: 180 } }, open ? "v" : ">", " ", label), /* @__PURE__ */ React.createElement(SkillSideEffect, { value: meta["side-effect"] }), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { flex: 1 } }, item.description), (item.problems || []).length > 0 && /* @__PURE__ */ React.createElement("span", { className: "pill red", title: item.problems.join("; ") }, "problem")), "check" in item && /* @__PURE__ */ React.createElement(SkillHealth, { item, unusedDays }), open && children);
}
function SkillsCheckPanel({ run, onNote, reload }) {
  const [busy, setBusy] = React.useState(false);
  const start = async () => {
    setBusy(true);
    try {
      await skillPost("/api/skills/check");
      onNote({ ok: true, text: "a staleness run started: it reads every skill's pins and may fetch for a few minutes" });
      setTimeout(reload, 15e3);
    } catch (e) {
      onNote({ ok: false, text: String(e.message || e) });
    }
    setBusy(false);
  };
  const r = run || null;
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "staleness", sub: "every approved skill's pins and what it rests on, checked read-only once a day" }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap", ...SKILL_MONO } }, !r && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "no run yet"), r && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "last run ", skillWhen(r.at), r.running ? " (running)" : ""), r && !r.running && /* @__PURE__ */ React.createElement("span", null, r.checked, " checked, ", /* @__PURE__ */ React.createElement("span", { style: { color: r.stale ? "var(--red)" : "inherit" } }, r.stale, " stale"), ", ", r.updates, " upstream changed, ", r.unknown, " items not checkable", r.errors ? `, ${r.errors} errors` : ""), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", disabled: busy, onClick: start }, "check now")));
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
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", style: { maxWidth: 1100 } }, note && /* @__PURE__ */ React.createElement(SysNote, { msg: note }), /* @__PURE__ */ React.createElement(SkillsCheckPanel, { run: data.check_run, onNote: setNote, reload: load }), /* @__PURE__ */ React.createElement(SysPanel, { title: "drafts waiting", sub: `${data.drafts.length} from the cousins; a private cousin's are in its inspector` }, data.drafts.length === 0 && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "none"), data.drafts.map((d) => {
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
      return /* @__PURE__ */ React.createElement(
        SkillRow,
        {
          key,
          label: sk.name,
          item: sk,
          open: open === key,
          onToggle: () => toggle(key),
          unusedDays: data.unused_days
        },
        /* @__PURE__ */ React.createElement(
          SkillApproved,
          {
            showUrl: urls.base,
            removalUrl: `${urls.base}/removal`,
            removeUrl: `${urls.base}/remove`,
            onDone: done
          }
        )
      );
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
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "skills"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 } }, note && /* @__PURE__ */ React.createElement(SysNote, { msg: note }), data.error && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: data.error } }), data.lane === "opencode" && /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "this lane loads no skills"), /* @__PURE__ */ React.createElement("span", { className: "muted", style: SKILL_MONO }, "repositories: ", (data.repos || []).join(", ") || "none", " (cousin settings: skill repos); loads at the next start"), (data.loaded || []).map((sk) => {
    const key = `loaded/${sk.plugin}/${sk.name}`;
    const url = `${base}/approved/${skillPath(sk.plugin, sk.name)}`;
    return /* @__PURE__ */ React.createElement(
      SkillRow,
      {
        key,
        label: `${sk.plugin}:${sk.name}`,
        item: sk,
        open: open === key,
        onToggle: () => toggle(key),
        unusedDays: data.unused_days
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
Object.assign(window, {
  SkillsSystemView,
  CousinSkillsPanel,
  SkillDraftReview,
  SkillApproved,
  SkillOutside,
  SkillHealth,
  SkillsCheckPanel
});
