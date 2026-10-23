// Skills: this package's own file (index.html's package block). Its routes
// are cousin_lib/console/routes_skills.py; the spec is
// docs/reference/skills.md.
//
// System > skills shows the shared set, each cousin's folders in the
// personal and work repositories, and the drafts waiting; a panel in each
// cousin's inspector shows what that cousin loads and its drafts, a private
// cousin's included. Import, approve, reject and remove exist only here, and
// each of the last three acts on the diff this page showed (its sha256),
// never on a newer one. Importing a skill from outside is the operator's
// act: the inspector's import form, never a cousin's tool.
//
// An approved skill also shows its health (cousin_lib/skills_check.py):
// stale when one of its pins or what it rests on changed, upstream changed
// for a skill from outside, unused after 30 days, and its last use. System
// > skills shows the last staleness run and starts one now.

const SKILL_MONO = { fontFamily: "var(--mono)", fontSize: 12 };
const SKILL_TONES = { "read-only": "green", "writes-local": "amber", "outward": "red" };

async function skillPost(path, body) {
  const { r, d } = await apiSend("POST", path, body || {});
  if (!r.ok) throw new Error((d && d.error) || `HTTP ${r.status}`);
  return d;
}

function skillPath(...parts) {
  return parts.map(p => encodeURIComponent(p)).join("/");
}

function SkillSideEffect({ value }) {
  return <span className={`pill ${SKILL_TONES[value] || "gray"}`}
               title="what the skill can change; an outward step stops at the operator's go">
    {value || "side-effect?"}
  </span>;
}

function SkillDiff({ text }) {
  if (!text) return <div className="muted" style={SKILL_MONO}>no difference from what is approved</div>;
  return <pre style={{ ...SKILL_MONO, whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto",
                       background: "var(--bg-0)", padding: 10, borderRadius: 6, margin: 0 }}>
    {text.split("\n").map((line, i) => (
      <div key={i} style={{ color: line.startsWith("+") && !line.startsWith("+++") ? "var(--green)"
                                 : line.startsWith("-") && !line.startsWith("---") ? "var(--red)"
                                 : "var(--fg-2)" }}>{line || " "}</div>
    ))}
  </pre>;
}

// The hosts and the reads pins show even when absent ("not declared") or
// declared empty ("none"): the approve view leads with them.
function SkillMeta({ metadata }) {
  const m = metadata || {};
  const keys = ["scope", "context", "hosts", "reads", "rests-on", "should-trigger", "must-not-trigger", "source"];
  const always = ["hosts", "reads"];
  const has = (k) => Object.prototype.hasOwnProperty.call(m, k);
  const rows = keys.filter(k => always.includes(k) || (has(k) && m[k] !== ""));
  return <div style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px", ...SKILL_MONO }}>
    {rows.map(k => <React.Fragment key={k}>
      <span className="muted">{k}</span>
      {!has(k) ? <span className="muted">not declared</span>
               : m[k] === "" ? <span className="muted">none</span>
               : <span style={{ color: "var(--fg-1)", overflowWrap: "anywhere" }}>{m[k]}</span>}
    </React.Fragment>)}
  </div>;
}

function SkillTrigger({ result }) {
  if (!result) return <span className="muted" style={SKILL_MONO}>not run on this version</span>;
  const line = (key, label) => {
    const r = result[key] || {};
    return <div key={key} style={SKILL_MONO}>
      <span className={`pill ${r.pass ? "green" : "red"}`}>{r.pass ? "pass" : "fail"}</span>
      <span style={{ marginLeft: 8 }}>{label}: "{r.prompt}"</span>
      <span className="muted" style={{ marginLeft: 8 }}>fired {r.fired || "nothing"}</span>
    </div>;
  };
  return <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
    {line("should-trigger", "should fire")}
    {line("must-not-trigger", "must not fire")}
    <span className="muted" style={SKILL_MONO}>on {result.model}, {result.at}</span>
  </div>;
}

// A draft from outside: where it came from, its licence (the file, to
// read), the files an agent could run, the source fetched again (as
// published past the frontmatter, or not), and the dry run the operator
// allows after reading the scripts, then the job that ran it.
function SkillOutside({ d, base, onAllowed }) {
  const o = d.outside;
  const [up, setUp] = React.useState(null);
  const [showLicence, setShowLicence] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    setUp(null);
    apiSend("GET", `${base}/upstream`).then(({ r, d: x }) =>
      setUp(r.ok ? x : { error: (x && x.error) || `HTTP ${r.status}` }));
  }, [base, d.diff_sha256]);
  const run = o.dry_run;
  const scripts = o.scripts || [];
  const row = (label, value) => <div style={{ display: "contents" }}>
    <span className="muted">{label}</span><span style={{ color: "var(--fg-1)", overflowWrap: "anywhere" }}>{value}</span>
  </div>;
  return <div data-skill-outside style={{ display: "flex", flexDirection: "column", gap: 6, ...SKILL_MONO }}>
    <div style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px" }}>
      {row("from outside", `${o.url || "?"} @ ${(o.commit || "?").slice(0, 12)} : ${o.folder || "?"}`)}
      {row("licence", <span>{o.license || "none"} ({o.license_file || "no licence file"}){" "}
        {o.license_text && <button className="btn ghost" style={{ fontSize: 10, padding: "0 6px" }}
                                   onClick={() => setShowLicence(s => !s)}>{showLicence ? "hide" : "read it"}</button>}
      </span>)}
      {row("can run", scripts.length ? scripts.map(s => `${s.path} (${s.why})`).join("; ") : "nothing")}
      {row("source now", !up ? <span className="muted">fetching it again...</span>
        : up.error ? <span style={{ color: "var(--red)" }}>{up.error}</span>
        : up.differ.length ? <span style={{ color: "var(--red)" }}>not as published: {up.differ.join(", ")}</span>
        : <span><span className="pill green">as published</span> past the frontmatter the import writes</span>)}
      {up && !up.error && up.not_kept.length > 0 && row("not kept", up.not_kept.map(
        k => (up.upstream || {})[k] ? `${k} (${up.upstream[k]})` : k).join(", "))}
      {row("dry run", <span>
        task: "{o.task || "none"}"{" "}
        {!run && <SysConfirmButton label="allow a dry run" tone="primary" disabled={busy || !o.task}
                                   confirmLabel="click again: I read its scripts; the cousin may run the task once"
                                   onConfirm={async () => {
                                     setBusy(true); setErr(null);
                                     try {
                                       const res = await skillPost(`${base}/dry-run/allow`, { diff_sha256: d.diff_sha256 });
                                       onAllowed(res.dry_run, res.told);
                                     } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
                                   }} />}
        {run && run.job == null && <span className="muted">allowed by {run.allowed_by} at {run.allowed_at}; waiting for the cousin's job</span>}
        {run && run.job != null && <span>
          <span className={`pill ${run.pass ? "green" : "red"}`}>{run.pass ? "pass" : "fail"}</span>
          {" "}job #{run.job} {run.status}, exit {String(run.exit_code)}{run.log ? ` · log ${run.log}` : ""}
        </span>}
      </span>)}
    </div>
    {showLicence && <pre style={{ ...SKILL_MONO, whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto",
                                  background: "var(--bg-0)", padding: 10, borderRadius: 6, margin: 0 }}>{o.license_text}</pre>}
    {err && <SysNote msg={{ ok: false, text: err }} />}
  </div>;
}

// One draft under review: what it can change first, then what keeps it
// from approval, its metadata, the diff and the trigger test; for a draft
// from outside, where it came from and its dry run too.
function SkillDraftReview({ slug, name, onDone }) {
  const [d, setD] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [told, setTold] = React.useState(null);
  const base = `/api/cousins/${skillPath(slug)}/skills/drafts/${skillPath(name)}`;
  React.useEffect(() => {
    setD(null); setErr(null);
    apiGet(base).then(x => x ? setD(x) : setErr("this draft cannot be read"));
  }, [base]);
  const act = async (fn) => {
    setBusy(true); setErr(null);
    try { await fn(); } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };
  if (err && !d) return <SysNote msg={{ ok: false, text: err }} />;
  if (!d) return <div className="muted" style={SKILL_MONO}>loading...</div>;
  const blocked = (d.problems || []).length > 0;
  // approval needs a trigger test on this very version; a failed one is
  // the operator's call, with its own confirm
  const untested = !d.trigger;
  // a draft from outside also needs a dry run on this version, its job's
  // result; a failed one is the operator's call too
  const run = d.outside ? d.outside.dry_run : null;
  const unrun = !!d.outside && !(run && run.job != null);
  const missed = (!!d.trigger && !d.trigger.pass) || (!!run && run.job != null && !run.pass);
  return <div style={{ display: "flex", flexDirection: "column", gap: 10, paddingLeft: 12,
                       borderLeft: "2px solid var(--hair)" }}>
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", ...SKILL_MONO }}>
      <SkillSideEffect value={(d.metadata || {})["side-effect"]} />
      <span>goes to {d.repo || "?"}:{d.folder || "?"}/{d.name}</span>
      <span className="muted" style={{ overflowWrap: "anywhere" }}>files: {(d.files || []).join(", ")}</span>
    </div>
    {blocked && <SysNote msg={{ ok: false, text: d.problems.join("; ") }} />}
    <SkillMeta metadata={d.metadata} />
    {d.outside && <SkillOutside d={d} base={base} onAllowed={(entry, ok) => {
      setD(x => ({ ...x, outside: { ...x.outside, dry_run: entry } }));
      setTold(ok ? `${slug} was told to run it` : `${slug} could not be told: ask it in the chat`);
    }} />}
    {told && <span className="muted" style={SKILL_MONO}>{told}</span>}
    <SkillDiff text={d.diff} />
    <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
      <button className="btn" disabled={busy}
              onClick={() => act(async () => {
                const res = await skillPost(`${base}/trigger-test`);
                setD(x => ({ ...x, trigger: res.trigger }));
              })}>{busy ? "running..." : "run the trigger test"}</button>
      <SkillTrigger result={d.trigger} />
    </div>
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
      <SysConfirmButton label={missed ? "approve anyway" : "approve"}
                        confirmLabel={missed ? "click again: approve despite a failed check"
                                             : "click again: commit it and load it at the next start"}
                        tone="primary" small={false} disabled={busy || blocked || !d.diff || untested || unrun}
                        onConfirm={() => act(async () => {
                          const res = await skillPost(`${base}/approve`, { diff_sha256: d.diff_sha256 });
                          onDone({ ok: true, text: `approved ${d.name}: ${res.note}` });
                        })} />
      <input className="txt" value={reason} onChange={e => setReason(e.target.value)}
             placeholder="why it is rejected (optional)" style={{ flex: 1, minWidth: 180 }} />
      <SysConfirmButton label="reject" small={false} disabled={busy}
                        onConfirm={() => act(async () => {
                          await skillPost(`${base}/reject`, { reason });
                          onDone({ ok: true, text: `rejected ${d.name}` });
                        })} />
    </div>
    {err && <SysNote msg={{ ok: false, text: err }} />}
  </div>;
}

// One approved skill: its text, and its removal, shown as a diff first.
function SkillApproved({ showUrl, removalUrl, removeUrl, onDone }) {
  const [s, setS] = React.useState(null);
  const [removal, setRemoval] = React.useState(null);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    setS(null); setRemoval(null); setErr(null);
    apiGet(showUrl).then(x => x ? setS(x) : setErr("this skill cannot be read"));
  }, [showUrl]);
  if (err) return <SysNote msg={{ ok: false, text: err }} />;
  if (!s) return <div className="muted" style={SKILL_MONO}>loading...</div>;
  return <div style={{ display: "flex", flexDirection: "column", gap: 8, paddingLeft: 12,
                       borderLeft: "2px solid var(--hair)" }}>
    <span className="muted" style={SKILL_MONO}>files: {(s.files || []).join(", ")}</span>
    <pre style={{ ...SKILL_MONO, whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto",
                  background: "var(--bg-0)", padding: 10, borderRadius: 6, margin: 0 }}>{s.text}</pre>
    {!removal && <button className="btn ghost" style={{ alignSelf: "flex-start" }}
                         onClick={() => apiGet(removalUrl).then(x => x ? setRemoval(x) : setErr("no removal diff"))}>
      remove...
    </button>}
    {removal && <>
      <SkillDiff text={removal.diff} />
      <SysConfirmButton label="remove it" confirmLabel="click again: commit the removal" small={false}
                        onConfirm={async () => {
                          try {
                            const res = await skillPost(removeUrl, { diff_sha256: removal.diff_sha256 });
                            onDone({ ok: true, text: `removed: ${res.note}` });
                          } catch (e) { setErr(String(e.message || e)); }
                        }} />
    </>}
  </div>;
}

function skillWhen(iso) {
  return iso ? String(iso).replace("T", " ").slice(0, 16) : "never";
}

// An approved skill's health: its last check, an upstream change, the
// unused flag and its last use. Drafts carry none of it.
function SkillHealth({ item, unusedDays }) {
  const check = item.check;
  const use = item.usage || {};
  return <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap", ...SKILL_MONO, fontSize: 11 }}>
    {!check && <span className="pill gray" title="the staleness run has not reached it yet">not checked yet</span>}
    {check && check.stale && <span className="pill red" title={`changed: ${(check.changed || []).join(", ")}`}>stale</span>}
    {check && !check.stale && <span className="pill green" title={`checked ${skillWhen(check.at)}`}>ok</span>}
    {check && check.update && <span className="pill amber" title="its source upstream changed: a new import, by the operator">upstream changed</span>}
    {item.unused && <span className="pill gray" title={`no use for ${unusedDays || 30} days: consider removing it`}>unused {unusedDays || 30}d</span>}
    <span className="muted">used {skillWhen(use.last)}{use.count ? ` (${use.count}x)` : ""}</span>
    {check && <span className="muted">checked {skillWhen(check.at)}{check.unknown ? `, ${check.unknown} not checkable` : ""}</span>}
    {check && check.stale && <span style={{ color: "var(--red)" }}>changed: {(check.changed || []).join(", ")}</span>}
  </div>;
}

function SkillRow({ label, item, open, onToggle, children, unusedDays }) {
  const meta = item.metadata || {};
  return <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: "10px 12px",
                       border: "1px solid var(--hair)", borderRadius: 8, background: "var(--bg-1)" }}>
    <div onClick={onToggle} style={{ display: "flex", flexDirection: "column", gap: 6, cursor: "pointer" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", ...SKILL_MONO }}>
        <span style={{ color: "var(--fg-0)", fontWeight: 600 }}>{open ? "v" : ">"} {label}</span>
        <SkillSideEffect value={meta["side-effect"]} />
        {(item.problems || []).length > 0 && <span className="pill red" title={item.problems.join("; ")}>problem</span>}
      </div>
      {item.description && <div style={{ color: "var(--fg-1)", fontSize: 13, lineHeight: 1.45 }}>{item.description}</div>}
    </div>
    {"check" in item && <SkillHealth item={item} unusedDays={unusedDays} />}
    {open && children}
  </div>;
}

// The last staleness run and the button that starts one now (read-only,
// rate-limited: the console refuses a second within 5 minutes).
function SkillsCheckPanel({ run, onNote, reload }) {
  const [busy, setBusy] = React.useState(false);
  const start = async () => {
    setBusy(true);
    try {
      await skillPost("/api/skills/check");
      onNote({ ok: true, text: "a staleness run started: it reads every skill's pins and may fetch for a few minutes" });
      setTimeout(reload, 15000);
    } catch (e) { onNote({ ok: false, text: String(e.message || e) }); }
    setBusy(false);
  };
  const r = run || null;
  return <SysPanel title="staleness" sub="every approved skill's pins and what it rests on, checked read-only once a day">
    <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap", ...SKILL_MONO }}>
      {!r && <span className="muted">no run yet</span>}
      {r && <span className="muted">last run {skillWhen(r.at)}{r.running ? " (running)" : ""}</span>}
      {r && !r.running && <span>{r.checked} checked, <span style={{ color: r.stale ? "var(--red)" : "inherit" }}>{r.stale} stale</span>, {r.updates} upstream changed, {r.unknown} items not checkable{r.errors ? `, ${r.errors} errors` : ""}</span>}
      <button className="btn ghost" disabled={busy} onClick={start}>check now</button>
    </div>
  </SysPanel>;
}

function SkillsSystemView() {
  const [data, setData] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const load = () => apiGet("/api/skills").then(d => setData(d || { repos: [], drafts: [] }));
  React.useEffect(() => { load(); }, []);
  const done = (msg) => { setNote(msg); setOpen(null); load(); };
  const toggle = (key) => setOpen(o => (o === key ? null : key));
  if (!data) return <div className="wrap-pad muted">loading skills...</div>;
  return <div className="wrap-pad" style={{ maxWidth: 1100 }}>
    {note && <SysNote msg={note} />}
    <SkillsCheckPanel run={data.check_run} onNote={setNote} reload={load} />
    <SysPanel title="drafts waiting" sub={`${data.drafts.length} from the cousins; a private cousin's are in its inspector`}>
      {data.drafts.length === 0 && <span className="muted" style={SKILL_MONO}>none</span>}
      {data.drafts.map(d => {
        const key = `draft/${d.cousin}/${d.name}`;
        return <SkillRow key={key} label={`${d.cousin}: ${d.name}`} item={d} open={open === key}
                         onToggle={() => toggle(key)}>
          <SkillDraftReview slug={d.cousin} name={d.name} onDone={done} />
        </SkillRow>;
      })}
    </SysPanel>
    {data.repos.map(repo => (
      <SysPanel key={repo.repo} title={repo.repo}
                sub={!repo.exists ? "no repository yet: the first approval creates it"
                     : repo.trusted ? `approved at ${String(repo.approved).slice(0, 10)}`
                     : "main has moved past its last approval: nothing loads from it"}>
        {repo.exists && repo.folders.length === 0 && <span className="muted" style={SKILL_MONO}>no skills</span>}
        {repo.folders.map(f => (
          <div key={f.folder} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <SectionLabel style={{ marginTop: 6 }}>{f.folder === "shared" ? "shared (every cousin)" : f.folder}</SectionLabel>
            {f.skills.map(sk => {
              const key = `${repo.repo}/${f.folder}/${sk.name}`;
              const urls = f.folder === "shared"
                ? { base: `/api/skills/shared/${skillPath(sk.name)}` }
                : { base: `/api/cousins/${skillPath(f.folder)}/skills/approved/${skillPath(repo.repo, sk.name)}` };
              return <SkillRow key={key} label={sk.name} item={sk} open={open === key} onToggle={() => toggle(key)}
                               unusedDays={data.unused_days}>
                <SkillApproved showUrl={urls.base} removalUrl={`${urls.base}/removal`}
                               removeUrl={`${urls.base}/remove`} onDone={done} />
              </SkillRow>;
            })}
          </div>
        ))}
      </SysPanel>
    ))}
  </div>;
}

// The operator's import of a skill published outside into this cousin's
// drafts: the source pinned to a full commit and the draft's own keys. The
// draft then shows below for review, the dry run and approval.
// the keys that say what a skill reaches: blank declares nothing, "" declares none
const SKILL_DECLARED = ["hosts", "reads", "rests_on"];

function SkillImportForm({ slug, targets, onDone }) {
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const where = targets || {};
  const scopes = ["own", "shared"].filter(s => (where[s] || []).length);
  const [f, setF] = React.useState({ source: "", scope: "own", context: "", side_effect: "read-only",
                                     name: "", should_trigger: "", must_not_trigger: "", dry_run_task: "",
                                     hosts: "", reads: "", rests_on: "", license: "" });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const aim = (scope) => ({ scope, context: (where[scope] || [])[0] || "" });
  if (!scopes.length) return <span className="muted" style={SKILL_MONO}>
    no repository this cousin loads takes a skill from outside (personal or private)
  </span>;
  if (!open) return <button className="btn ghost" style={{ alignSelf: "flex-start" }}
                            onClick={() => { setF({ ...f, ...aim(scopes[0]) }); setOpen(true); }}>
    import a skill from outside...</button>;
  const field = (k, label, ph) => <label key={k} style={{ display: "flex", gap: 8, alignItems: "baseline", ...SKILL_MONO }}>
    <span className="muted" style={{ minWidth: 150 }}>{label}</span>
    <input className="txt" style={{ flex: 1 }} value={f[k]} placeholder={ph || ""} onChange={set(k)} />
  </label>;
  const submit = async () => {
    setBusy(true); setErr(null);
    const body = { ...f };
    for (const k of [...SKILL_DECLARED, "license"]) {
      const v = body[k].trim();
      if (!v) delete body[k];
      else body[k] = SKILL_DECLARED.includes(k) && v === '""' ? "" : v;
    }
    try {
      const res = await skillPost(`/api/cousins/${skillPath(slug)}/skills/import`, body);
      setOpen(false);
      onDone({ ok: true, text: `imported: ${res.text}` });
    } catch (e) { setErr(String(e.message || e)); }
    setBusy(false);
  };
  return <div style={{ display: "flex", flexDirection: "column", gap: 6, paddingLeft: 12, borderLeft: "2px solid var(--hair)" }}>
    {field("source", "source", "https://github.com/<owner>/<repo>@<full commit>:<folder>")}
    <label style={{ display: "flex", gap: 8, alignItems: "baseline", ...SKILL_MONO }}>
      <span className="muted" style={{ minWidth: 150 }}>scope, repository</span>
      <select className="sel" value={f.scope} onChange={(e) => setF({ ...f, ...aim(e.target.value) })}>
        {scopes.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      <select className="sel" value={f.context} onChange={set("context")}>
        {(where[f.scope] || []).map(r => <option key={r} value={r}>{r}</option>)}
      </select>
    </label>
    <label style={{ display: "flex", gap: 8, alignItems: "baseline", ...SKILL_MONO }}>
      <span className="muted" style={{ minWidth: 150 }}>side effect</span>
      <select className="sel" value={f.side_effect} onChange={set("side_effect")}>
        {["read-only", "writes-local"].map(s => <option key={s} value={s}>{s}</option>)}
      </select>
    </label>
    {field("name", "draft name", "default: the skill's own")}
    {field("should_trigger", "should trigger", "a prompt that should make it fire")}
    {field("must_not_trigger", "must not trigger", "a prompt that must not")}
    {field("dry_run_task", "dry-run task", "the sample task its dry run does")}
    {field("hosts", "hosts", 'every host it may contact, comma-separated; "" for none')}
    {field("reads", "reads", "repo@<full commit>:path or path#sha256:<hex>, comma-separated")}
    {field("rests_on", "rests on", "paths, tool versions and memory topics, comma-separated")}
    {field("license", "licence", "SPDX id; default: what its licence file reads as")}
    <span className="muted" style={SKILL_MONO}>
      fetched at that commit, byte for byte; an open licence only; never outward, never into work;
      shared: every cousin that loads personal gets it; hosts, reads, rests on: blank declares nothing
    </span>
    <div style={{ display: "flex", gap: 8 }}>
      <button className="btn" disabled={busy || !f.source || !f.context} onClick={submit}>{busy ? "fetching..." : "import"}</button>
      <button className="btn ghost" onClick={() => setOpen(false)}>cancel</button>
    </div>
    {err && <SysNote msg={{ ok: false, text: err }} />}
  </div>;
}

function CousinSkillsPanel({ cousin }) {
  const c = cousin;
  const [data, setData] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const base = `/api/cousins/${skillPath(c.slug)}/skills`;
  const load = () => apiGet(base).then(d => setData(d));
  React.useEffect(() => { setData(null); setNote(null); setOpen(null); if (!c.remote) load(); }, [c.slug]);
  if (c.remote || !data) return null;
  const done = (msg) => { setNote(msg); setOpen(null); load(); };
  const toggle = (key) => setOpen(o => (o === key ? null : key));
  return <>
    <SectionLabel style={{ marginTop: 20 }}>skills</SectionLabel>
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {note && <SysNote msg={note} />}
      {data.error && <SysNote msg={{ ok: false, text: data.error }} />}
      {data.lane === "opencode" && <span className="muted" style={SKILL_MONO}>this lane loads no skills</span>}
      <span className="muted" style={SKILL_MONO}>
        repositories: {(data.repos || []).join(", ") || "none"} (cousin settings: skill repos); loads at the next start
      </span>
      {(data.loaded || []).map(sk => {
        const key = `loaded/${sk.plugin}/${sk.name}`;
        const url = `${base}/approved/${skillPath(sk.plugin, sk.name)}`;
        return <SkillRow key={key} label={`${sk.plugin}:${sk.name}`} item={sk} open={open === key}
                         onToggle={() => toggle(key)} unusedDays={data.unused_days}>
          <SkillApproved showUrl={url} removalUrl={`${url}/removal`} removeUrl={`${url}/remove`} onDone={done} />
        </SkillRow>;
      })}
      {(data.loaded || []).length === 0 && <span className="muted" style={SKILL_MONO}>no approved skills</span>}
      {data.lane !== "opencode" && <SkillImportForm slug={c.slug} targets={data.import_targets} onDone={done} />}
      {(data.drafts || []).map(d => {
        const key = `draft/${d.name}`;
        return <SkillRow key={key} label={`draft: ${d.name}`} item={d} open={open === key}
                         onToggle={() => toggle(key)}>
          <SkillDraftReview slug={c.slug} name={d.name} onDone={done} />
        </SkillRow>;
      })}
    </div>
  </>;
}

registerSlot("system.tabs", { id: "skills", label: "skills", order: 45,
                              render: () => <SkillsSystemView /> });
registerSlot("inspector.panels", { id: "skills", order: 35,
                                   render: ({ cousin }) => <CousinSkillsPanel cousin={cousin} /> });

Object.assign(window, { SkillsSystemView, CousinSkillsPanel, SkillDraftReview, SkillApproved, SkillOutside,
                        SkillHealth, SkillsCheckPanel, SkillImportForm });
