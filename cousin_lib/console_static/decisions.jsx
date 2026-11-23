// decisions.jsx: the Decisions view (registerView "decisions"), this
// package's own file (index.html's package block). Its routes are
// cousin_lib/console/routes_decisions.py; the concept is docs/decisions.md.
//
// A cousin asks the operator a question with its `decision` tool; this view
// lists the open ones across every cousin, grouped by cousin and then by
// group (a skill's stop point is a group too), each with its question, why,
// its options as buttons (the recommended one marked) and a free-text box.
// An answer goes to POST /api/decisions/<id>/answer. The answered ones are
// below, collapsed, with who answered and when, and what each still owes
// ("memory not written", "turn not delivered"): the console's event poller
// retries those, and "retry now" posts /api/decisions/redeliver. The view refetches on the
// decisions-refresh event (re-dispatched by app.jsx as
// "fw-decisions-refresh") and on a 15 s fallback poll.

// ---- decision helpers (pure: no React, no fetch; tests run them in node) ----

// The title a group of decisions shows: a skill's stop point, else the
// group's name, else nothing (a decision on its own).
function decisionGroupTitle(d) {
  if (!d) return "";
  if (d.skill) return "skill " + d.skill + ", stop " + (d.stop || "-");
  return d.grp ? "group " + d.grp : "";
}

// The open decisions as [{cousin, groups: [{key, title, rows}]}]: cousins
// in the order their oldest open question was asked, and within a cousin
// its groups the same way; a decision with no group is a group of one.
function decisionGroups(rows) {
  const cousins = [];
  const byCousin = {};
  for (const d of (rows || []).filter(r => r.status === "open").sort((a, b) => a.id - b.id)) {
    let c = byCousin[d.cousin];
    if (!c) { c = byCousin[d.cousin] = { cousin: d.cousin, groups: [], byKey: {} }; cousins.push(c); }
    const key = d.grp ? "g:" + d.grp : "d:" + d.id;
    let g = c.byKey[key];
    if (!g) { g = c.byKey[key] = { key, title: decisionGroupTitle(d), rows: [] }; c.groups.push(g); }
    g.rows.push(d);
  }
  return cousins.map(c => ({ cousin: c.cousin, groups: c.groups }));
}

// An answer as it is said back: "a: option text", a free-text note after
// it when there is one, or the free text alone.
function decisionAnswerText(d) {
  if (!d) return "";
  const opt = (d.options || []).find(o => o.key === d.answer_option);
  const free = (d.answer || "").trim();
  if (opt) return opt.key + ": " + opt.text + (free && free !== opt.text ? " (" + free + ")" : "");
  return free;
}

// What an answered decision still owes, in words; "" when nothing. One
// that can never reach its cousin says so with the reason: only "retry
// now" tries it again.
function decisionOwes(d) {
  if (!d || d.status !== "answered") return "";
  if (d.undeliverable) return "undeliverable: " + d.undeliverable;
  const out = [];
  if (!d.remembered) out.push("memory not written");
  if (!d.delivered) out.push("turn not delivered");
  return out.join(", ");
}
// ---- end decision helpers ----

function DecisionCard({ d, busy, onAnswer }) {
  const [text, setText] = React.useState("");
  const send = (option) => onAnswer(d, option, text.trim());
  return (
    <div className="panel" data-decision={d.id} style={{ marginBottom: 8, padding: "10px 14px" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" }}>#{d.id}</span>
        <span style={{ fontWeight: 600 }}>{d.question}</span>
      </div>
      {d.why && <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>{d.why}</div>}
      {(d.options || []).length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
          {d.options.map(o => {
            const recommended = o.key === d.recommendation;
            return (
              <button key={o.key} className={"btn" + (recommended ? " primary" : "")} disabled={busy}
                      data-option={o.key} onClick={() => send(o.key)}
                      title={recommended ? "recommended by @" + d.cousin : "answer " + o.key}>
                {o.key}: {o.text}{recommended ? " (recommended)" : ""}
              </button>
            );
          })}
        </div>
      )}
      {d.recommendation && !(d.options || []).some(o => o.key === d.recommendation) && (
        <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>recommended: {d.recommendation}</div>
      )}
      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <input className="txt" style={{ flex: 1 }} value={text} onChange={e => setText(e.target.value)}
               placeholder={(d.options || []).length ? "a note with the option, or an answer of your own" : "your answer"} />
        <button className="btn" disabled={busy || !text.trim()} onClick={() => send("")}
                title="answer with this text only">answer</button>
      </div>
    </div>
  );
}

function DecisionsView() {
  const [rows, setRows] = React.useState(null);
  const [open, setOpen] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [status, setStatus] = React.useState(null);
  const [showAnswered, setShowAnswered] = React.useState(false);

  const pull = React.useCallback(async () => {
    const d = await apiGet("/api/decisions");
    if (d) { setRows(d.decisions || []); setOpen(d.open || 0); }
  }, []);

  React.useEffect(() => {
    pull();
    const on = () => pull();
    window.addEventListener("fw-decisions-refresh", on);
    const id = setInterval(() => { if (!document.hidden) pull(); }, 15000);
    return () => { window.removeEventListener("fw-decisions-refresh", on); clearInterval(id); };
  }, [pull]);

  const answer = async (d, option, text) => {
    setBusy(true);
    try {
      const body = {};
      if (option) body.option = option;
      if (text) body.answer = text;
      const { r, d: out } = await apiSend("POST", `/api/decisions/${d.id}/answer`, body);
      if (!r.ok || out.error) throw new Error(out.error || `HTTP ${r.status}`);
      const how = { delivered: "sent to @" + d.cousin, waiting: "kept until its group is answered",
                    pending: "kept; delivery is retried",
                    undeliverable: "kept, but it cannot reach @" + d.cousin }[out.decision.delivery] || "";
      setStatus({ ok: true, msg: `answered #${d.id}` + (how ? ", " + how : "") });
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally { setBusy(false); }
  };

  const retry = async () => {
    setBusy(true);
    try {
      const { r, d: out } = await apiSend("POST", "/api/decisions/redeliver", {});
      if (!r.ok || out.error) throw new Error(out.error || `HTTP ${r.status}`);
      const sent = (out.tried || []).filter(t => t.ok).length;
      setStatus({ ok: true, msg: `retried: ${sent} of ${(out.tried || []).length} turns sent` });
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally { setBusy(false); }
  };

  if (rows === null) return <div className="wrap-pad muted">loading decisions...</div>;
  const grouped = decisionGroups(rows);
  const answered = rows.filter(r => r.status === "answered");
  const owing = answered.filter(d => decisionOwes(d)).length;
  return (
    <div className="wrap-pad" data-decisions-view>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span className="eyebrow" data-decisions-open>{open} open {open === 1 ? "decision" : "decisions"}</span>
        <span style={{ flex: 1 }} />
        {owing > 0 && <button className="btn" disabled={busy} onClick={retry} data-decisions-retry
                              title="retry the memories and turns answers still owe">retry now ({owing} owed)</button>}
        {status && <span style={{ fontSize: 11, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" }}>{status.msg}</span>}
      </div>
      {grouped.length === 0 && <div className="muted">No cousin is waiting on a decision.</div>}
      {grouped.map(c => (
        <div key={c.cousin} style={{ marginBottom: 16 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>@{c.cousin}</div>
          {c.groups.map(g => (
            <div key={g.key} data-decision-group={g.key} style={{ marginLeft: g.title ? 8 : 0 }}>
              {g.title && <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)", margin: "4px 0" }}>
                {g.title}{g.rows.length > 1 ? " (answers go back together)" : ""}</div>}
              {g.rows.map(d => <DecisionCard key={d.id} d={d} busy={busy} onAnswer={answer} />)}
            </div>
          ))}
        </div>
      ))}
      {answered.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <button className="btn ghost" onClick={() => setShowAnswered(!showAnswered)}>
            {showAnswered ? "hide" : "show"} {answered.length} answered
          </button>
          {showAnswered && (
            <table className="data" style={{ marginTop: 8 }} data-decisions-answered>
              <thead><tr><th>id</th><th>cousin</th><th>question</th><th>answer</th><th>by</th><th>when</th><th>owed</th></tr></thead>
              <tbody>
                {answered.map(d => (
                  <tr key={d.id}>
                    <td className="mono">#{d.id}</td>
                    <td>@{d.cousin}</td>
                    <td>{d.question}{decisionGroupTitle(d) ? <div className="muted" style={{ fontSize: 11 }}>{decisionGroupTitle(d)}</div> : null}</td>
                    <td>{decisionAnswerText(d)}</td>
                    <td>{d.answered_by}</td>
                    <td className="mono">{(d.answered_at || "").replace("T", " ").slice(0, 16)}</td>
                    <td className={decisionOwes(d) ? "" : "muted"}>{decisionOwes(d) || "nothing"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { DecisionsView, decisionGroups, decisionGroupTitle, decisionAnswerText,
                        decisionOwes });

registerView({ id: "decisions", label: "Decisions", icon: I.list, order: 40,
               render: (props) => <DecisionsView {...props} /> });
