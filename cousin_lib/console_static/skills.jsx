// Skills: this package's own file (index.html's package block). Its routes
// are cousin_lib/console/routes_skills.py; the spec is
// docs/reference/skills.md.
//
// System > skills shows the shared set, each cousin's folders in the
// personal and work repositories, and the drafts waiting; a panel in each
// cousin's inspector shows what that cousin loads and its drafts, a private
// cousin's included. Approve, reject and remove exist only here, and each
// acts on the diff this page showed (its sha256), never on a newer one.

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

function SkillRow({ label, item, open, onToggle, children }) {
  const meta = item.metadata || {};
  return <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
    <div onClick={onToggle} style={{ display: "flex", gap: 10, alignItems: "baseline", cursor: "pointer", ...SKILL_MONO }}>
      <span style={{ color: "var(--fg-0)", minWidth: 180 }}>{open ? "v" : ">"} {label}</span>
      <SkillSideEffect value={meta["side-effect"]} />
      <span className="muted" style={{ flex: 1 }}>{item.description}</span>
      {(item.problems || []).length > 0 && <span className="pill red" title={item.problems.join("; ")}>problem</span>}
    </div>
    {open && children}
  </div>;
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
          <div key={f.folder} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="muted" style={SKILL_MONO}>{f.folder === "shared" ? "shared (every cousin)" : f.folder}</span>
            {f.skills.map(sk => {
              const key = `${repo.repo}/${f.folder}/${sk.name}`;
              const urls = f.folder === "shared"
                ? { base: `/api/skills/shared/${skillPath(sk.name)}` }
                : { base: `/api/cousins/${skillPath(f.folder)}/skills/approved/${skillPath(repo.repo, sk.name)}` };
              return <SkillRow key={key} label={sk.name} item={sk} open={open === key} onToggle={() => toggle(key)}>
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
                         onToggle={() => toggle(key)}>
          <SkillApproved showUrl={url} removalUrl={`${url}/removal`} removeUrl={`${url}/remove`} onDone={done} />
        </SkillRow>;
      })}
      {(data.loaded || []).length === 0 && <span className="muted" style={SKILL_MONO}>no approved skills</span>}
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

Object.assign(window, { SkillsSystemView, CousinSkillsPanel, SkillDraftReview, SkillApproved, SkillOutside });
