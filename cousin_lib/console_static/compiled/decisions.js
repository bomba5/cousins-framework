function decisionGroupTitle(d) {
  if (!d) return "";
  if (d.skill) return "skill " + d.skill + ", stop " + (d.stop || "-");
  return d.grp ? "group " + d.grp : "";
}
function decisionGroups(rows) {
  const cousins = [];
  const byCousin = {};
  for (const d of (rows || []).filter((r) => r.status === "open").sort((a, b) => a.id - b.id)) {
    let c = byCousin[d.cousin];
    if (!c) {
      c = byCousin[d.cousin] = { cousin: d.cousin, groups: [], byKey: {} };
      cousins.push(c);
    }
    const key = d.grp ? "g:" + d.grp : "d:" + d.id;
    let g = c.byKey[key];
    if (!g) {
      g = c.byKey[key] = { key, title: decisionGroupTitle(d), rows: [] };
      c.groups.push(g);
    }
    g.rows.push(d);
  }
  return cousins.map((c) => ({ cousin: c.cousin, groups: c.groups }));
}
function decisionAnswerText(d) {
  if (!d) return "";
  const opt = (d.options || []).find((o) => o.key === d.answer_option);
  const free = (d.answer || "").trim();
  if (opt) return opt.key + ": " + opt.text + (free && free !== opt.text ? " (" + free + ")" : "");
  return free;
}
function decisionOwes(d) {
  if (!d || d.status !== "answered") return "";
  const out = [];
  if (!d.remembered) out.push("memory not written");
  if (!d.delivered) out.push("turn not delivered");
  return out.join(", ");
}
function DecisionCard({ d, busy, onAnswer }) {
  const [text, setText] = React.useState("");
  const send = (option) => onAnswer(d, option, text.trim());
  return /* @__PURE__ */ React.createElement("div", { className: "panel", "data-decision": d.id, style: { marginBottom: 8, padding: "10px 14px" } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "baseline" } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" } }, "#", d.id), /* @__PURE__ */ React.createElement("span", { style: { fontWeight: 600 } }, d.question)), d.why && /* @__PURE__ */ React.createElement("div", { className: "muted", style: { marginTop: 4, fontSize: 12 } }, d.why), (d.options || []).length > 0 && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 } }, d.options.map((o) => {
    const recommended = o.key === d.recommendation;
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        key: o.key,
        className: "btn" + (recommended ? " primary" : ""),
        disabled: busy,
        "data-option": o.key,
        onClick: () => send(o.key),
        title: recommended ? "recommended by @" + d.cousin : "answer " + o.key
      },
      o.key,
      ": ",
      o.text,
      recommended ? " (recommended)" : ""
    );
  })), d.recommendation && !(d.options || []).some((o) => o.key === d.recommendation) && /* @__PURE__ */ React.createElement("div", { className: "muted", style: { marginTop: 6, fontSize: 12 } }, "recommended: ", d.recommendation), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 8 } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { flex: 1 },
      value: text,
      onChange: (e) => setText(e.target.value),
      placeholder: (d.options || []).length ? "a note with the option, or an answer of your own" : "your answer"
    }
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      disabled: busy || !text.trim(),
      onClick: () => send(""),
      title: "answer with this text only"
    },
    "answer"
  )));
}
function DecisionsView() {
  const [rows, setRows] = React.useState(null);
  const [open, setOpen] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [status, setStatus] = React.useState(null);
  const [showAnswered, setShowAnswered] = React.useState(false);
  const pull = React.useCallback(async () => {
    const d = await apiGet("/api/decisions");
    if (d) {
      setRows(d.decisions || []);
      setOpen(d.open || 0);
    }
  }, []);
  React.useEffect(() => {
    pull();
    const on = () => pull();
    window.addEventListener("fw-decisions-refresh", on);
    const id = setInterval(() => {
      if (!document.hidden) pull();
    }, 15e3);
    return () => {
      window.removeEventListener("fw-decisions-refresh", on);
      clearInterval(id);
    };
  }, [pull]);
  const answer = async (d, option, text) => {
    setBusy(true);
    try {
      const body = {};
      if (option) body.option = option;
      if (text) body.answer = text;
      const { r, d: out } = await apiSend("POST", `/api/decisions/${d.id}/answer`, body);
      if (!r.ok || out.error) throw new Error(out.error || `HTTP ${r.status}`);
      const how = {
        delivered: "sent to @" + d.cousin,
        waiting: "kept until its group is answered",
        pending: "kept; delivery is retried"
      }[out.decision.delivery] || "";
      setStatus({ ok: true, msg: `answered #${d.id}` + (how ? ", " + how : "") });
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  const retry = async () => {
    setBusy(true);
    try {
      const { r, d: out } = await apiSend("POST", "/api/decisions/redeliver", {});
      if (!r.ok || out.error) throw new Error(out.error || `HTTP ${r.status}`);
      const sent = (out.tried || []).filter((t) => t.ok).length;
      setStatus({ ok: true, msg: `retried: ${sent} of ${(out.tried || []).length} turns sent` });
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  if (rows === null) return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad muted" }, "loading decisions...");
  const grouped = decisionGroups(rows);
  const answered = rows.filter((r) => r.status === "answered");
  const owing = answered.filter((d) => decisionOwes(d)).length;
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", "data-decisions-view": true }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 12 } }, /* @__PURE__ */ React.createElement("span", { className: "eyebrow", "data-decisions-open": true }, open, " open ", open === 1 ? "decision" : "decisions"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), owing > 0 && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      disabled: busy,
      onClick: retry,
      "data-decisions-retry": true,
      title: "retry the memories and turns answers still owe"
    },
    "retry now (",
    owing,
    " owed)"
  ), status && /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" } }, status.msg)), grouped.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "muted" }, "No cousin is waiting on a decision."), grouped.map((c) => /* @__PURE__ */ React.createElement("div", { key: c.cousin, style: { marginBottom: 16 } }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 6 } }, "@", c.cousin), c.groups.map((g) => /* @__PURE__ */ React.createElement("div", { key: g.key, "data-decision-group": g.key, style: { marginLeft: g.title ? 8 : 0 } }, g.title && /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)", margin: "4px 0" } }, g.title, g.rows.length > 1 ? " (answers go back together)" : ""), g.rows.map((d) => /* @__PURE__ */ React.createElement(DecisionCard, { key: d.id, d, busy, onAnswer: answer })))))), answered.length > 0 && /* @__PURE__ */ React.createElement("div", { style: { marginTop: 20 } }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => setShowAnswered(!showAnswered) }, showAnswered ? "hide" : "show", " ", answered.length, " answered"), showAnswered && /* @__PURE__ */ React.createElement("table", { className: "data", style: { marginTop: 8 }, "data-decisions-answered": true }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "id"), /* @__PURE__ */ React.createElement("th", null, "cousin"), /* @__PURE__ */ React.createElement("th", null, "question"), /* @__PURE__ */ React.createElement("th", null, "answer"), /* @__PURE__ */ React.createElement("th", null, "by"), /* @__PURE__ */ React.createElement("th", null, "when"), /* @__PURE__ */ React.createElement("th", null, "owed"))), /* @__PURE__ */ React.createElement("tbody", null, answered.map((d) => /* @__PURE__ */ React.createElement("tr", { key: d.id }, /* @__PURE__ */ React.createElement("td", { className: "mono" }, "#", d.id), /* @__PURE__ */ React.createElement("td", null, "@", d.cousin), /* @__PURE__ */ React.createElement("td", null, d.question, decisionGroupTitle(d) ? /* @__PURE__ */ React.createElement("div", { className: "muted", style: { fontSize: 11 } }, decisionGroupTitle(d)) : null), /* @__PURE__ */ React.createElement("td", null, decisionAnswerText(d)), /* @__PURE__ */ React.createElement("td", null, d.answered_by), /* @__PURE__ */ React.createElement("td", { className: "mono" }, (d.answered_at || "").replace("T", " ").slice(0, 16)), /* @__PURE__ */ React.createElement("td", { className: decisionOwes(d) ? "" : "muted" }, decisionOwes(d) || "nothing")))))));
}
Object.assign(window, {
  DecisionsView,
  decisionGroups,
  decisionGroupTitle,
  decisionAnswerText,
  decisionOwes
});
registerView({
  id: "decisions",
  label: "Decisions",
  icon: I.list,
  order: 40,
  render: (props) => /* @__PURE__ */ React.createElement(DecisionsView, { ...props })
});
