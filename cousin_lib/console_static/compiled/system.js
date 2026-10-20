function SysNote({ msg }) {
  if (!msg) return null;
  return /* @__PURE__ */ React.createElement("div", { style: { margin: "6px 0" } }, /* @__PURE__ */ React.createElement("span", { className: `pill ${msg.ok ? "green" : "red"}` }, msg.ok ? "ok" : "refused"), /* @__PURE__ */ React.createElement("span", { className: "mono", style: { marginLeft: 8, fontSize: 11, color: "var(--fg-2)" } }, msg.text));
}
function SysConfirmButton({
  label,
  confirmLabel = "click again to confirm",
  onConfirm,
  tone = "danger",
  disabled = false,
  small = true
}) {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4e3);
    return () => clearTimeout(t);
  }, [armed]);
  const style = small ? { fontSize: 10, padding: "2px 8px", minHeight: 18 } : void 0;
  return /* @__PURE__ */ React.createElement(
    "button",
    {
      className: `btn ${tone}`,
      style,
      disabled,
      onClick: () => {
        if (!armed) {
          setArmed(true);
          return;
        }
        setArmed(false);
        onConfirm();
      }
    },
    armed ? confirmLabel : label
  );
}
async function sysSend(method, path, body) {
  try {
    const { r, d } = await apiSend(method, path, body);
    return { ok: r.ok && d && d.ok !== false, d: d || {}, status: r.status };
  } catch (e) {
    return { ok: false, d: { error: String(e.message || e) }, status: 0 };
  }
}
function SysRestartOffer({ restart, applies }) {
  const [msg, setMsg] = React.useState(null);
  if (!restart) {
    return applies ? /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("span", { className: "hint" }, applies)) : null;
  }
  const go = async () => {
    const res = await sysSend("POST", restart.route || "/api/admin/restart/framework");
    if (!res.ok) {
      setMsg({ ok: false, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    setMsg({ ok: true, text: res.d.supervised === false ? "the console is not supervised: this restart is a stop; start it again by hand" : "console restart kicked off" });
    window.dispatchEvent(new CustomEvent("fw-restart", { detail: { target: "console", etaSeconds: res.d.eta_seconds || 4 } }));
  };
  return /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("span", { className: "hint" }, applies, ". Needs a restart of: ", (restart.services || []).join(", "), ".", restart.note ? " " + restart.note + "." : ""), (restart.services || []).includes("console") && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "restart console", onConfirm: go })), /* @__PURE__ */ React.createElement(SysNote, { msg }));
}
function SysPanel({ title, sub, right, children }) {
  return /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, title), sub && /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg-3)", fontSize: 11, marginLeft: 8 } }, sub), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), right), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 14, display: "flex", flexDirection: "column", gap: 10 } }, children));
}
function SysSupervisorPanel() {
  const [st, setSt] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [busy, setBusy] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet("/api/system/supervisor");
    if (d) {
      setSt(d);
      setFailed(false);
    } else setFailed(true);
  }, []);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 5e3);
    return () => clearInterval(id);
  }, [load]);
  const act = async (op, child, extra) => {
    setBusy(child + op);
    const path = op === "start" ? "/api/system/supervisor/start" : "/api/system/supervisor/stop";
    const res = await sysSend("POST", path, { child, ...extra || {} });
    setBusy(null);
    setMsg(res.ok ? { ok: true, text: `${child}: ${res.d.state || op}` } : { ok: false, text: res.d.error || `HTTP ${res.status}` });
    load();
  };
  const reload = async () => {
    const res = await sysSend("POST", "/api/system/supervisor/reload");
    setMsg(res.ok ? { ok: true, text: `rescanned: added ${(res.d.added || []).join(", ") || "-"}; removed ${(res.d.removed || []).join(", ") || "-"}` } : { ok: false, text: res.d.error || `HTTP ${res.status}` });
    load();
  };
  const restartConsole = async () => {
    const res = await sysSend("POST", "/api/admin/restart/framework");
    setMsg(res.ok ? { ok: true, text: "console restart kicked off" } : { ok: false, text: res.d.error || "refused" });
    if (res.ok) window.dispatchEvent(new CustomEvent("fw-restart", { detail: { target: "console", etaSeconds: res.d.eta_seconds || 4 } }));
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  return /* @__PURE__ */ React.createElement(
    SysPanel,
    {
      title: "supervisor",
      sub: st && st.running ? `pid ${st.pid} · up since ${st.started || "-"}` : "",
      right: st && st.running && /* @__PURE__ */ React.createElement("button", { className: "btn", style: small, onClick: reload }, "reload")
    },
    /* @__PURE__ */ React.createElement(SysNote, { msg }),
    failed && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: "the console did not answer /api/system/supervisor" + (st ? "; showing the last answer" : "") } }),
    !st ? !failed && /* @__PURE__ */ React.createElement("span", { className: "muted" }, "loading...") : !st.running ? /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("span", { className: "hint" }, st.running === false ? "no cousin-supervisor runs for this install" : "the supervisor did not answer", ": ", st.reason)) : /* @__PURE__ */ React.createElement("div", { className: "table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data sys-children" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "child"), /* @__PURE__ */ React.createElement("th", null, "state"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "pid"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "restarts"), /* @__PURE__ */ React.createElement("th", null, "since"), /* @__PURE__ */ React.createElement("th", null, "reason"), /* @__PURE__ */ React.createElement("th", null))), /* @__PURE__ */ React.createElement("tbody", null, st.children.map((row) => {
      const a = row.actions || {};
      return /* @__PURE__ */ React.createElement("tr", { key: row.name }, /* @__PURE__ */ React.createElement("td", { className: "mono" }, row.name), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement(StatePill, { state: row.state === "failing" ? "failed" : row.state === "backoff" ? "degraded" : row.state })), /* @__PURE__ */ React.createElement("td", { className: "num" }, row.pid || "-"), /* @__PURE__ */ React.createElement("td", { className: "num" }, row.restarts), /* @__PURE__ */ React.createElement("td", { className: "muted mono" }, row.since || "-"), /* @__PURE__ */ React.createElement("td", { className: "muted" }, row.reason || (row.last_exit != null ? `last exit ${row.last_exit}` : "")), /* @__PURE__ */ React.createElement("td", { style: { whiteSpace: "nowrap" } }, a.start && row.state !== "running" && /* @__PURE__ */ React.createElement("button", { className: "btn", style: small, disabled: busy === row.name + "start", onClick: () => act("start", row.name) }, "start"), a.stop && row.state !== "stopped" && /* @__PURE__ */ React.createElement(
        SysConfirmButton,
        {
          label: "stop",
          confirmLabel: a.confirm === "loops" ? "stops every loop: confirm" : "click again to stop",
          disabled: busy === row.name + "stop",
          onConfirm: () => act("stop", row.name, a.confirm ? { confirm: a.confirm } : null)
        }
      ), a.restart && /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "restart", onConfirm: restartConsole }), !a.start && !a.stop && !a.restart && /* @__PURE__ */ React.createElement("span", { className: "muted", title: a.why }, "follows its runner")));
    })))),
    /* @__PURE__ */ React.createElement("span", { className: "hint mono", style: { fontSize: 11, color: "var(--fg-3)" } }, "the console restarts through its own route and is never stopped from here; a bridge follows its runner cousin")
  );
}
function SchedulesPanel({ slug, cousins }) {
  const fixed = !!slug;
  const [rows, setRows] = React.useState([]);
  const [history, setHistory] = React.useState(false);
  const [target, setTarget] = React.useState(slug || "");
  const [when, setWhen] = React.useState("in 30m");
  const [prompt, setPrompt] = React.useState("");
  const [msg, setMsg] = React.useState(null);
  const local = (cousins || []).filter((c) => !c.remote && c.type !== "remote");
  const load = React.useCallback(async () => {
    const base = fixed ? `/api/cousins/${slug}/schedules` : "/api/system/schedules";
    const d = await apiGet(history ? base + "?all=1" : base);
    if (d) setRows(d.schedules || []);
  }, [slug, fixed, history]);
  React.useEffect(() => {
    load();
  }, [load]);
  React.useEffect(() => {
    if (!target && local.length) setTarget(local[0].slug);
  }, [local.length]);
  const add = async () => {
    const res = await sysSend("POST", `/api/cousins/${target}/schedules`, { when, prompt });
    if (res.ok) {
      setPrompt("");
      setMsg({ ok: true, text: `#${res.d.schedule.id} for ${target} at ${res.d.schedule.when}` });
    } else setMsg({ ok: false, text: res.d.error || `HTTP ${res.status}` });
    load();
  };
  const cancel = async (row) => {
    const res = await sysSend("POST", `/api/cousins/${row.cousin}/schedules/${row.id}/cancel`);
    setMsg(res.ok ? { ok: true, text: `#${row.id} cancelled` } : { ok: false, text: res.d.error || "refused" });
    load();
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  return /* @__PURE__ */ React.createElement(
    SysPanel,
    {
      title: "schedules",
      sub: "one-shot prompts (cousin-schedule)",
      right: /* @__PURE__ */ React.createElement("label", { className: "mono", style: { fontSize: 11 } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: history, onChange: (e) => setHistory(e.target.checked) }), " history")
    },
    /* @__PURE__ */ React.createElement(SysNote, { msg }),
    rows.length === 0 ? /* @__PURE__ */ React.createElement("span", { className: "muted", style: { fontSize: 12 } }, "nothing ", history ? "scheduled" : "pending") : /* @__PURE__ */ React.createElement("div", { className: "table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data sys-schedules" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", { className: "num" }, "#"), !fixed && /* @__PURE__ */ React.createElement("th", null, "cousin"), /* @__PURE__ */ React.createElement("th", null, "when"), /* @__PURE__ */ React.createElement("th", null, "status"), /* @__PURE__ */ React.createElement("th", null, "prompt"), /* @__PURE__ */ React.createElement("th", null))), /* @__PURE__ */ React.createElement("tbody", null, rows.map((row) => /* @__PURE__ */ React.createElement("tr", { key: row.id }, /* @__PURE__ */ React.createElement("td", { className: "num" }, row.id), !fixed && /* @__PURE__ */ React.createElement("td", { className: "mono" }, row.cousin), /* @__PURE__ */ React.createElement("td", { className: "mono" }, row.when), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement(Pill, { tone: row.status === "pending" ? "amber" : row.status === "fired" ? "green" : "gray" }, row.status)), /* @__PURE__ */ React.createElement("td", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }, row.prompt), /* @__PURE__ */ React.createElement("td", null, row.status === "pending" && /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "cancel", onConfirm: () => cancel(row) }))))))),
    /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" } }, !fixed && /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: target, onChange: (e) => setTarget(e.target.value) }, local.map((c) => /* @__PURE__ */ React.createElement("option", { key: c.slug, value: c.slug }, c.slug))), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "txt",
        style: { width: 170 },
        value: when,
        onChange: (e) => setWhen(e.target.value),
        placeholder: "in 30m / tomorrow 06:30 / 2026-10-01T09:00"
      }
    ), /* @__PURE__ */ React.createElement(
      "textarea",
      {
        className: "txt",
        style: { flex: "1 1 240px", minHeight: 40 },
        value: prompt,
        onChange: (e) => setPrompt(e.target.value),
        placeholder: "the prompt the cousin receives, marked as scheduled"
      }
    ), /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: small, disabled: !target || !prompt.trim() || !when.trim(), onClick: add }, "schedule"))
  );
}
function SysOutboxPanel() {
  const [rows, setRows] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet("/api/system/outbox");
    setRows(d ? d.rows || [] : []);
  }, []);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 15e3);
    return () => clearInterval(id);
  }, [load]);
  const tone = { pending: "amber", delivered: "green", gave_up: "red" };
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "outbox", sub: "messages to external peers sent again under the same id until they land (up to 14 min)" }, rows === null ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "loading...") : rows.length === 0 ? /* @__PURE__ */ React.createElement("span", { className: "muted", style: { fontSize: 12 } }, "nothing kept: every message to an external peer was confirmed on its first try") : /* @__PURE__ */ React.createElement("div", { className: "table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "msg_id"), /* @__PURE__ */ React.createElement("th", null, "from"), /* @__PURE__ */ React.createElement("th", null, "to"), /* @__PURE__ */ React.createElement("th", null, "state"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "tries"), /* @__PURE__ */ React.createElement("th", null, "last error"), /* @__PURE__ */ React.createElement("th", null, "message"))), /* @__PURE__ */ React.createElement("tbody", null, rows.map((r) => /* @__PURE__ */ React.createElement("tr", { key: r.id }, /* @__PURE__ */ React.createElement("td", { className: "mono", title: r.msg_id }, r.msg_id.slice(0, 8)), /* @__PURE__ */ React.createElement("td", { className: "mono" }, r.sender), /* @__PURE__ */ React.createElement("td", { className: "mono" }, r.dest), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement(Pill, { tone: tone[r.state] || "gray" }, r.state.replace("_", " "))), /* @__PURE__ */ React.createElement("td", { className: "num" }, r.attempts), /* @__PURE__ */ React.createElement("td", { style: { wordBreak: "break-word" } }, r.last_error || ""), /* @__PURE__ */ React.createElement("td", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }, r.message.length > 160 ? r.message.slice(0, 157) + "..." : r.message)))))));
}
function SysUsersPanel() {
  const [st, setSt] = React.useState(null);
  const [name, setName] = React.useState("");
  const [pw, setPw] = React.useState("");
  const [pw2, setPw2] = React.useState("");
  const [msg, setMsg] = React.useState(null);
  const [resetFor, setResetFor] = React.useState(null);
  const [removeFor, setRemoveFor] = React.useState(null);
  const [typed, setTyped] = React.useState("");
  const load = React.useCallback(async () => setSt(await apiGet("/api/system/users")), []);
  React.useEffect(() => {
    load();
  }, [load]);
  const add = async () => {
    if (pw !== pw2) {
      setMsg({ ok: false, text: "the passwords differ" });
      return;
    }
    const body = { name, password: pw };
    setPw("");
    setPw2("");
    const res = await sysSend("POST", "/api/system/users", body);
    if (res.ok) {
      setName("");
      setMsg({ ok: true, text: res.d.logged_in ? `${res.d.user} added; this browser is now logged in as ${res.d.user}` : `${res.d.user} added` });
    } else setMsg({ ok: false, text: res.d.error || `HTTP ${res.status}` });
    load();
  };
  const reset = async (user, value) => {
    const res = await sysSend("POST", `/api/system/users/${encodeURIComponent(user)}/password`, { password: value });
    setMsg(res.ok ? { ok: true, text: `password reset for ${user}; their sessions end` } : { ok: false, text: res.d.error || "refused" });
    setResetFor(null);
  };
  const remove = async (user) => {
    const res = await sysSend("POST", `/api/system/users/${encodeURIComponent(user)}/remove`, { confirm: typed });
    setMsg(res.ok ? { ok: true, text: `${user} removed` } : { ok: false, text: res.d.error || "refused" });
    setRemoveFor(null);
    setTyped("");
    load();
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const users = st && st.users || [];
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "console users", sub: st && !st.configured ? "auth not configured: the network guard is the only boundary" : "passwords are write-only" }, /* @__PURE__ */ React.createElement(SysNote, { msg }), users.length > 0 && /* @__PURE__ */ React.createElement("table", { className: "data sys-users" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "user"), /* @__PURE__ */ React.createElement("th", null))), /* @__PURE__ */ React.createElement("tbody", null, users.map((u) => /* @__PURE__ */ React.createElement("tr", { key: u }, /* @__PURE__ */ React.createElement("td", { className: "mono" }, u, st.me === u && /* @__PURE__ */ React.createElement("span", { className: "muted" }, " (you)")), /* @__PURE__ */ React.createElement("td", { style: { display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" } }, st.me !== u && resetFor !== u && /* @__PURE__ */ React.createElement("button", { className: "btn", style: small, onClick: () => setResetFor(u) }, "reset password"), resetFor === u && /* @__PURE__ */ React.createElement(
    SysPasswordReset,
    {
      user: u,
      onCancel: () => setResetFor(null),
      onSubmit: (v) => reset(u, v)
    }
  ), st.me !== u && users.length > 1 && removeFor !== u && /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: small, onClick: () => {
    setRemoveFor(u);
    setTyped("");
  } }, "remove"), removeFor === u && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { width: 150 },
      value: typed,
      onChange: (e) => setTyped(e.target.value),
      placeholder: `type ${u} to confirm`,
      autoComplete: "off"
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: small, disabled: typed !== u, onClick: () => remove(u) }, "remove ", u), /* @__PURE__ */ React.createElement("button", { className: "btn", style: small, onClick: () => setRemoveFor(null) }, "cancel")), st.me === u && /* @__PURE__ */ React.createElement("span", { className: "muted", style: { fontSize: 11 } }, "change your own password in Settings")))))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("input", { className: "txt", style: { width: 150 }, value: name, onChange: (e) => setName(e.target.value), placeholder: "new user", autoComplete: "off" }), /* @__PURE__ */ React.createElement("input", { className: "txt", style: { width: 170 }, type: "password", value: pw, onChange: (e) => setPw(e.target.value), placeholder: "password (8+)", autoComplete: "new-password" }), /* @__PURE__ */ React.createElement("input", { className: "txt", style: { width: 170 }, type: "password", value: pw2, onChange: (e) => setPw2(e.target.value), placeholder: "again", autoComplete: "new-password" }), /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: small, disabled: !name || !pw || !pw2, onClick: add }, "add user")), st && !st.configured && /* @__PURE__ */ React.createElement("span", { className: "hint", style: { fontSize: 11, color: "var(--fg-3)" } }, "the first user closes the console to everyone without a session; this browser is logged in as that user."));
}
function SysPasswordReset({ user, onSubmit, onCancel }) {
  const [pw, setPw] = React.useState("");
  const [pw2, setPw2] = React.useState("");
  const [err, setErr] = React.useState(null);
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const send = () => {
    if (!pw || !pw2) return;
    if (pw !== pw2) {
      setErr("the passwords differ");
      return;
    }
    const value = pw;
    setPw("");
    setPw2("");
    setErr(null);
    onSubmit(value);
  };
  return /* @__PURE__ */ React.createElement("span", { style: { display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { width: 160 },
      type: "password",
      value: pw,
      autoFocus: true,
      autoComplete: "new-password",
      placeholder: `new password for ${user} (8+)`,
      onChange: (e) => setPw(e.target.value)
    }
  ), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { width: 120 },
      type: "password",
      value: pw2,
      autoComplete: "new-password",
      placeholder: "again",
      onChange: (e) => setPw2(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") send();
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: small, disabled: !pw || !pw2, onClick: send }, "reset"), /* @__PURE__ */ React.createElement("button", { className: "btn", style: small, onClick: () => {
    setPw("");
    setPw2("");
    onCancel();
  } }, "cancel"), err && /* @__PURE__ */ React.createElement("span", { className: "pill red" }, err));
}
const SYS_BACKUP_KEY = "console_system_backup_dest";
function SysBackupPanel({ cousins }) {
  const local = (cousins || []).filter((c) => !c.remote && c.type !== "remote").map((c) => c.slug).sort();
  const [dest, setDest] = React.useState(() => {
    try {
      return localStorage.getItem(SYS_BACKUP_KEY) || "";
    } catch (_e) {
      return "";
    }
  });
  const [pick, setPick] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const [started, setStarted] = React.useState([]);
  const chosen = pick || local;
  const toggle = (slug) => {
    const cur = new Set(chosen);
    if (cur.has(slug)) cur.delete(slug);
    else cur.add(slug);
    setPick([...cur].sort());
  };
  const run = async () => {
    try {
      localStorage.setItem(SYS_BACKUP_KEY, dest);
    } catch (_e) {
    }
    const res = await sysSend("POST", "/api/system/backup", { dest, slugs: pick ? chosen : "all" });
    if (!res.ok) {
      setMsg({ ok: false, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    const busy = Object.entries(res.d.busy || {});
    setMsg({ ok: true, text: `backing up ${Object.keys(res.d.ops).length} into ${res.d.dest}` + (busy.length ? `; busy: ${busy.map(([s]) => s).join(", ")}` : "") });
    setStarted(Object.keys(res.d.ops));
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "backup", sub: "cousin-backup: databases, memory and core files" }, /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "destination"), /* @__PURE__ */ React.createElement("input", { className: "txt", value: dest, onChange: (e) => setDest(e.target.value), placeholder: "/an/absolute/directory" }), /* @__PURE__ */ React.createElement("span", { className: "hint" }, "absolute, an existing directory the console can write; never inside the install's cousins/, config/ or .secrets/. Each cousin lands in <dest>/<slug>/<date>/.")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, flexWrap: "wrap" } }, local.map((slug) => /* @__PURE__ */ React.createElement("label", { key: slug, className: "mono", style: { fontSize: 11 } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: chosen.includes(slug), onChange: () => toggle(slug) }), " ", slug))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: small, disabled: !dest.trim() || !chosen.length, onClick: run }, "back up now")), /* @__PURE__ */ React.createElement(SysNote, { msg }), started.map((slug) => /* @__PURE__ */ React.createElement("div", { key: slug }, /* @__PURE__ */ React.createElement("span", { className: "mono", style: { fontSize: 11 } }, slug), /* @__PURE__ */ React.createElement(LongOpStatus, { slug, kind: "backup" }))));
}
function SysAgentDefaultsPanel() {
  const [st, setSt] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [draft, setDraft] = React.useState({});
  const [msg, setMsg] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet("/api/system/agent-defaults");
    setFailed(!d);
    if (d) setSt(d);
    if (d) setDraft({
      default_model: d.values.default_model.source.startsWith("config") ? d.values.default_model.value : "",
      default_effort: d.values.default_effort.source.startsWith("config") ? d.values.default_effort.value : "",
      commit_attribution: d.values.commit_attribution.source.startsWith("config") ? String(d.values.commit_attribution.value) : ""
    });
  }, []);
  React.useEffect(() => {
    load();
  }, [load]);
  if (!st) return /* @__PURE__ */ React.createElement(SysPanel, { title: "agent defaults" }, failed ? /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: "the console did not answer /api/system/agent-defaults" } }) : /* @__PURE__ */ React.createElement("span", { className: "muted" }, "loading..."));
  const save = async () => {
    const body = { remove: [] };
    if (draft.default_model) body.default_model = draft.default_model;
    else body.remove.push("default_model");
    if (draft.default_effort) body.default_effort = draft.default_effort;
    else body.remove.push("default_effort");
    if (draft.commit_attribution !== "") body.commit_attribution = draft.commit_attribution === "true";
    else body.remove.push("commit_attribution");
    const res = await sysSend("POST", "/api/system/agent-defaults", body);
    setMsg(res.ok ? { ok: true, text: "saved to config/harness.toml" } : { ok: false, text: res.d.error || `HTTP ${res.status}` });
    if (res.ok) load();
  };
  const src = (key) => /* @__PURE__ */ React.createElement("span", { className: "hint" }, "now ", JSON.stringify(st.values[key].value), " · ", st.values[key].source);
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "agent defaults", sub: "config/harness.toml [agent], install-wide" }, st.error && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: st.error } }), !st.exists && /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: "config/harness.toml is absent: copy an example file first" } }), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "default_model"), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      list: "sys-model-list",
      value: draft.default_model || "",
      onChange: (e) => setDraft({ ...draft, default_model: e.target.value }),
      placeholder: "unset"
    }
  ), /* @__PURE__ */ React.createElement("datalist", { id: "sys-model-list" }, (st.choices.models || []).map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m }))), src("default_model")), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "default_effort"), /* @__PURE__ */ React.createElement("select", { className: "sel", value: draft.default_effort || "", onChange: (e) => setDraft({ ...draft, default_effort: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "unset"), st.choices.effort.map((e) => /* @__PURE__ */ React.createElement("option", { key: e, value: e }, e))), src("default_effort")), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "commit_attribution"), /* @__PURE__ */ React.createElement("select", { className: "sel", value: draft.commit_attribution, onChange: (e) => setDraft({ ...draft, commit_attribution: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "unset (built-in: true)"), /* @__PURE__ */ React.createElement("option", { value: "true" }, "true: the harness's own attribution"), /* @__PURE__ */ React.createElement("option", { value: "false" }, "false: no injected attribution")), src("commit_attribution"), /* @__PURE__ */ React.createElement("span", { className: "hint" }, "a cousin's own cousin.toml [agent] commit_attribution overrides this.")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: { fontSize: 10, padding: "2px 8px", minHeight: 18 }, disabled: !st.exists, onClick: save }, "save")), /* @__PURE__ */ React.createElement(SysNote, { msg }), /* @__PURE__ */ React.createElement(SysRestartOffer, { applies: st.applies }));
}
function sysFieldToInput(kind, value) {
  if (value === null || value === void 0) return "";
  if (kind === "list") return (value || []).join(", ");
  return String(value);
}
function sysInputToValue(kind, text, label) {
  const t = String(text).trim();
  if (t === "") return { remove: true };
  if (kind === "num" || kind === "int") {
    const n = Number(t);
    if (!Number.isFinite(n)) return { error: `${label} is not a number` };
    if (kind === "int" && !Number.isInteger(n)) return { error: `${label} must be a whole number` };
    return { value: n };
  }
  if (kind === "bool") return { value: t === "true" };
  if (kind === "list") return { value: t.split(",").map((s) => s.trim()).filter(Boolean) };
  return { value: t };
}
function SysTomlForm({ name, fields, values, onSaved, extraChanges }) {
  const sig = JSON.stringify([fields, values]);
  const initial = React.useMemo(() => Object.fromEntries(fields.map((f) => [f.label, sysFieldToInput(f.kind, values[f.label])])), [sig]);
  const [draft, setDraft] = React.useState(initial);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => setDraft(initial), [initial]);
  const dirty = fields.filter((f) => draft[f.label] !== initial[f.label]);
  const save = async () => {
    const changes = [];
    for (const f of dirty) {
      const v = sysInputToValue(f.kind, draft[f.label], f.label);
      if (v.error) {
        setMsg({ ok: false, text: v.error + "; nothing saved" });
        return;
      }
      changes.push(v.remove ? { table: f.table, key: f.key, remove: true } : { table: f.table, key: f.key, value: v.value });
    }
    const res = await sysSend("POST", `/api/system/config/${name}`, { changes: changes.concat(extraChanges || []) });
    setMsg(res.ok ? { ok: true, text: "saved" } : { ok: false, text: res.d.error || `HTTP ${res.status}` });
    if (res.ok && onSaved) onSaved(res.d.file);
  };
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 } }, fields.map((f) => /* @__PURE__ */ React.createElement("div", { key: f.label, className: "field" }, /* @__PURE__ */ React.createElement("label", null, f.label), f.kind === "bool" ? /* @__PURE__ */ React.createElement("select", { className: "sel", value: draft[f.label], onChange: (e) => setDraft({ ...draft, [f.label]: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "unset"), /* @__PURE__ */ React.createElement("option", { value: "true" }, "true"), /* @__PURE__ */ React.createElement("option", { value: "false" }, "false")) : /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft[f.label],
      placeholder: f.placeholder || "unset",
      onChange: (e) => setDraft({ ...draft, [f.label]: e.target.value })
    }
  ), f.hint && /* @__PURE__ */ React.createElement("span", { className: "hint" }, f.hint)))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn primary",
      style: { fontSize: 10, padding: "2px 8px", minHeight: 18 },
      disabled: !dirty.length && !(extraChanges || []).length,
      onClick: save
    },
    "save"
  )), /* @__PURE__ */ React.createElement(SysNote, { msg }));
}
function SysFileHead({ file }) {
  return /* @__PURE__ */ React.createElement("div", { className: "mono", style: { fontSize: 11, color: "var(--fg-3)" } }, file.path, " · ", file.exists ? "present" : "absent (saving creates it)", file.error && /* @__PURE__ */ React.createElement("div", { style: { marginTop: 4 } }, /* @__PURE__ */ React.createElement(Pill, { tone: "red" }, "error"), " ", file.error));
}
function SysMediaEditor({ file, reload }) {
  const [msg, setMsg] = React.useState(null);
  const fieldsFor = (kind) => [
    { table: kind, key: "url", label: "url", kind: "url", placeholder: "http(s)://... (unset: kind off)" },
    { table: kind, key: "model", label: "model", kind: "str" },
    { table: kind, key: "timeout_s", label: "timeout_s", kind: "num", placeholder: kind === "transcribe" ? "300" : "120" }
  ].concat(kind === "transcribe" ? [
    { table: kind, key: "language", label: "language", kind: "str", placeholder: "auto (or da, en, it...)" },
    { table: kind, key: "prompt", label: "prompt", kind: "str", hint: "words to expect: names, places" }
  ] : []);
  const secret = async (kind, value) => {
    const res = await sysSend("POST", `/api/system/config/media/${kind}/secret`, { value });
    setMsg(res.ok ? { ok: true, text: `${kind} key saved` } : { ok: false, text: res.d.error || "refused" });
    reload();
  };
  const clear = async (kind) => {
    const res = await sysSend("POST", `/api/system/config/media/${kind}/secret/clear`);
    setMsg(res.ok ? { ok: true, text: `${kind} key cleared` } : { ok: false, text: res.d.error || "refused" });
    reload();
  };
  const remove = async (kind) => {
    const res = await sysSend("POST", "/api/system/config/media", { changes: [], remove_tables: [kind] });
    setMsg(res.ok ? { ok: true, text: `[${kind}] removed` } : { ok: false, text: res.d.error || "refused" });
    reload();
  };
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "media", sub: "config/media.toml: image, voice, video providers, and transcribe (speech to text for Telegram voice notes)" }, /* @__PURE__ */ React.createElement(SysFileHead, { file }), /* @__PURE__ */ React.createElement(SysNote, { msg }), ["image", "voice", "video", "transcribe"].map((kind) => {
    const k = file.kinds[kind] || {};
    return /* @__PURE__ */ React.createElement("div", { key: kind, style: { borderTop: "1px solid var(--line-soft)", paddingTop: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { className: "eyebrow" }, kind), /* @__PURE__ */ React.createElement(Pill, { tone: k.url ? "green" : "gray" }, k.url ? "on" : "off"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), file.kinds[kind] && /* @__PURE__ */ React.createElement(SysConfirmButton, { label: `remove [${kind}]`, onConfirm: () => remove(kind) })), /* @__PURE__ */ React.createElement(SysTomlForm, { name: "media", fields: fieldsFor(kind), values: k, onSaved: reload }), /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, "key (sent as Bearer)"), /* @__PURE__ */ React.createElement(
      SecretField,
      {
        status: k.key || { set: false },
        placeholder: `paste the ${kind} provider key`,
        onSubmit: (v) => secret(kind, v),
        hint: k.key_file ? `key_file = ${k.key_file}` : "stored under config/media-keys/"
      }
    ), k.key && k.key.error && /* @__PURE__ */ React.createElement("span", { className: "hint" }, k.key.error), k.key_file && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "clear key", onConfirm: () => clear(kind) }))));
  }), /* @__PURE__ */ React.createElement(SysRestartOffer, { applies: file.applies, restart: file.restart }));
}
const SYS_EMBEDDING_FIELDS = [
  { table: "", key: "url", label: "url", kind: "url", hint: "required: takes {model, prompt}, returns {embedding}" },
  { table: "", key: "model", label: "model", kind: "str" },
  { table: "", key: "timeout_s", label: "timeout_s", kind: "num", placeholder: "10" },
  { table: "", key: "chunk_chars", label: "chunk_chars", kind: "int", placeholder: "2000" },
  { table: "", key: "chunk_overlap", label: "chunk_overlap", kind: "int", placeholder: "200" },
  { table: "options", key: "num_thread", label: "options.num_thread", kind: "int" },
  { table: "recall", key: "min_chars", label: "recall.min_chars", kind: "int", placeholder: "24" },
  { table: "recall", key: "min_score", label: "recall.min_score", kind: "num", placeholder: "0.45" },
  { table: "recall", key: "top", label: "recall.top", kind: "int", placeholder: "3" }
];
const SYS_HIVE_FIELDS = [
  { table: "", key: "enabled", label: "enabled", kind: "bool" },
  { table: "", key: "public_url", label: "public_url", kind: "url", hint: "the queen as nodes reach it; required when enabled" },
  { table: "", key: "checkin_seconds", label: "checkin_seconds", kind: "int", placeholder: "60" },
  { table: "", key: "home_cousin", label: "home_cousin", kind: "str", hint: "the local cousin a node's tell-home reaches" }
];
function SysPlainTomlEditor({ title, sub, name, file, fields, reload }) {
  return /* @__PURE__ */ React.createElement(SysPanel, { title, sub }, /* @__PURE__ */ React.createElement(SysFileHead, { file }), /* @__PURE__ */ React.createElement(SysTomlForm, { name, fields, values: file.values || {}, onSaved: reload }), /* @__PURE__ */ React.createElement(SysRestartOffer, { applies: file.applies, restart: file.restart }));
}
const SYS_PEER_FIELDS = (slug) => [
  { table: `peers.${slug}`, key: "url", label: "url", kind: "url", hint: "the peer's chat server or console base URL" },
  { table: `peers.${slug}`, key: "send_path", label: "send_path", kind: "str", placeholder: "/api/send (/peer/send with a token)" },
  { table: `peers.${slug}`, key: "name", label: "name", kind: "str", hint: "how its messages show here" },
  { table: `peers.${slug}`, key: "sender", label: "sender", kind: "str", hint: "the name that peer knows this install by" },
  { table: `peers.${slug}`, key: "reach", label: "reach", kind: "list", hint: "local cousins it may write to, comma separated" }
];
function SysPeersEditor({ file, reload }) {
  const [msg, setMsg] = React.useState(null);
  const [slug, setSlug] = React.useState("");
  const [url, setUrl] = React.useState("");
  const add = async () => {
    const res = await sysSend("POST", "/api/system/config/peers", { changes: [
      { table: `peers.${slug}`, key: "url", value: url },
      { table: `peers.${slug}`, key: "reach", value: [] }
    ] });
    setMsg(res.ok ? { ok: true, text: `${slug} added` } : { ok: false, text: res.d.error || "refused" });
    if (res.ok) {
      setSlug("");
      setUrl("");
    }
    reload();
  };
  const remove = async (s) => {
    const res = await sysSend("POST", "/api/system/config/peers", { changes: [], remove_tables: [`peers.${s}`] });
    setMsg(res.ok ? { ok: true, text: `${s} removed (its token files stay under config/peer-tokens/)` } : { ok: false, text: res.d.error || "refused" });
    reload();
  };
  const token = async (s, which, value) => {
    const res = await sysSend("POST", `/api/system/config/peers/${s}/secret`, { which, value });
    setMsg(res.ok ? { ok: true, text: `${s} ${which} token saved` } : { ok: false, text: res.d.error || "refused" });
    reload();
  };
  const clearToken = async (s, which) => {
    const res = await sysSend("POST", `/api/system/config/peers/${s}/secret/clear`, { which });
    setMsg(res.ok ? { ok: true, text: `${s} ${which} token cleared` } : { ok: false, text: res.d.error || "refused" });
    reload();
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "external peers", sub: "config/external-peers.toml: cousins on another install" }, /* @__PURE__ */ React.createElement(SysFileHead, { file }), /* @__PURE__ */ React.createElement(SysNote, { msg }), Object.entries(file.peers || {}).map(([s, p]) => /* @__PURE__ */ React.createElement("div", { key: s, style: { borderTop: "1px solid var(--line-soft)", paddingTop: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { className: "eyebrow" }, s), p.shadowed && /* @__PURE__ */ React.createElement(Pill, { tone: "amber" }, "a local cousin has this slug: the local one wins"), p.unknown_reach.length > 0 && /* @__PURE__ */ React.createElement(Pill, { tone: "amber" }, "reach names no local cousin: ", p.unknown_reach.join(", ")), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "remove peer", onConfirm: () => remove(s) })), /* @__PURE__ */ React.createElement(SysTomlForm, { name: "peers", fields: SYS_PEER_FIELDS(s), values: p, onSaved: reload }), [
    ["outbound", "token", "token_file", "signs what this install sends it"],
    ["inbound", "inbound_token", "inbound_token_file", "checks what it sends to POST /peer/send"]
  ].map(([which, stKey, fileKey, what]) => /* @__PURE__ */ React.createElement("div", { key: which, className: "field" }, /* @__PURE__ */ React.createElement("label", null, which, " token (", what, ")"), /* @__PURE__ */ React.createElement(
    SecretField,
    {
      status: p[stKey] || { set: false },
      placeholder: `paste the shared ${which} secret`,
      onSubmit: (v) => token(s, which, v),
      hint: p[fileKey] ? `${fileKey} = ${p[fileKey]}` : "stored under config/peer-tokens/"
    }
  ), p[stKey] && p[stKey].error && /* @__PURE__ */ React.createElement("span", { className: "hint" }, p[stKey].error), p[fileKey] && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(SysConfirmButton, { label: `clear ${which} token`, onConfirm: () => clearToken(s, which) })))))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, flexWrap: "wrap", borderTop: "1px solid var(--line-soft)", paddingTop: 8 } }, /* @__PURE__ */ React.createElement("input", { className: "txt", style: { width: 140 }, value: slug, onChange: (e) => setSlug(e.target.value), placeholder: "peer slug" }), /* @__PURE__ */ React.createElement("input", { className: "txt", style: { flex: "1 1 200px" }, value: url, onChange: (e) => setUrl(e.target.value), placeholder: "http://host:port" }), /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: small, disabled: !slug || !url, onClick: add }, "add peer")), /* @__PURE__ */ React.createElement(SysRestartOffer, { applies: file.applies, restart: file.restart }));
}
function SysTextFileEditor({ title, sub, file, route, reload, rows = 14, hint }) {
  const [text, setText] = React.useState(file.content);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => setText(file.content), [file.content, file.sha]);
  const save = async () => {
    const res = await sysSend("POST", route, { content: text, base_sha: file.sha });
    setMsg(res.ok ? { ok: true, text: "saved" + (res.d.backup ? `; the old version is in ${res.d.backup}` : "") } : { ok: false, text: res.d.error || `HTTP ${res.status}` });
    if (res.ok) reload();
  };
  return /* @__PURE__ */ React.createElement(SysPanel, { title, sub }, /* @__PURE__ */ React.createElement(SysFileHead, { file }), hint && /* @__PURE__ */ React.createElement("span", { className: "hint", style: { fontSize: 11, color: "var(--fg-3)" } }, hint), /* @__PURE__ */ React.createElement("textarea", { className: "txt code", rows, value: text, onChange: (e) => setText(e.target.value), spellCheck: false }), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "save", tone: "primary", disabled: text === file.content, onConfirm: save })), /* @__PURE__ */ React.createElement(SysNote, { msg }), /* @__PURE__ */ React.createElement(SysRestartOffer, { applies: file.applies, restart: file.restart }));
}
function SysAllowlistEditor({ file, reload }) {
  const [items, setItems] = React.useState(file.allow || []);
  const [add, setAdd] = React.useState("");
  const [msg, setMsg] = React.useState(null);
  const [saved, setSaved] = React.useState(null);
  React.useEffect(() => setItems(file.allow || []), [file.sha]);
  const save = async () => {
    const res = await sysSend("POST", "/api/system/allowlist", { allow: items, base_sha: file.sha });
    setMsg(res.ok ? { ok: true, text: "saved" } : { ok: false, text: res.d.error || `HTTP ${res.status}` });
    if (res.ok) {
      setSaved(res.d.restart);
      reload();
    }
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const dirty = JSON.stringify(items) !== JSON.stringify(file.allow || []);
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "network allowlist", sub: "config/net-allowlist.json: networks beyond loopback and the private ranges" }, /* @__PURE__ */ React.createElement(SysFileHead, { file }), /* @__PURE__ */ React.createElement("span", { className: "hint mono", style: { fontSize: 11, color: "var(--fg-3)" } }, "always allowed: ", file.builtin.join(", "), " · you are ", file.client, "; a list that would leave you out is refused"), items.map((cidr, i) => /* @__PURE__ */ React.createElement("div", { key: cidr + i, style: { display: "flex", gap: 8, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { className: "mono", style: { flex: 1 } }, cidr), /* @__PURE__ */ React.createElement(SysConfirmButton, { label: "remove", onConfirm: () => setItems(items.filter((_, j) => j !== i)) }))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8 } }, /* @__PURE__ */ React.createElement("input", { className: "txt", style: { width: 200 }, value: add, onChange: (e) => setAdd(e.target.value), placeholder: "100.64.0.0/10" }), /* @__PURE__ */ React.createElement("button", { className: "btn", style: small, disabled: !add.trim(), onClick: () => {
    setItems([...items, add.trim()]);
    setAdd("");
  } }, "add"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: small, disabled: !dirty, onClick: save }, "save")), /* @__PURE__ */ React.createElement(SysNote, { msg }), /* @__PURE__ */ React.createElement(SysRestartOffer, { applies: file.applies, restart: saved || file.restart }));
}
function SysCommandsView({ commands }) {
  return /* @__PURE__ */ React.createElement(SysPanel, { title: "agent and worker commands", sub: "read-only: a command line is code execution; edit it on the host" }, Object.entries(commands).map(([name, c]) => /* @__PURE__ */ React.createElement("div", { key: name, className: "field" }, /* @__PURE__ */ React.createElement("label", null, c.path), c.exists ? /* @__PURE__ */ React.createElement("pre", { className: "mono", style: { whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0, fontSize: 11 } }, c.content) : /* @__PURE__ */ React.createElement("span", { className: "hint" }, "absent"))));
}
function SysConfigEditors() {
  const [cfg, setCfg] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const load = React.useCallback(async () => {
    const d = await apiGet("/api/system/config");
    setFailed(!d);
    if (d) setCfg(d.files);
  }, []);
  React.useEffect(() => {
    load();
  }, [load]);
  if (!cfg) return /* @__PURE__ */ React.createElement(SysPanel, { title: "install config" }, failed ? /* @__PURE__ */ React.createElement(SysNote, { msg: { ok: false, text: "the console did not answer /api/system/config" } }) : /* @__PURE__ */ React.createElement("span", { className: "muted" }, "loading..."));
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SysMediaEditor, { file: cfg.media, reload: load }), /* @__PURE__ */ React.createElement(
    SysPlainTomlEditor,
    {
      title: "embedding",
      sub: "config/embedding.toml: the semantic leg of memory search",
      name: "embedding",
      file: cfg.embedding,
      fields: SYS_EMBEDDING_FIELDS,
      reload: load
    }
  ), /* @__PURE__ */ React.createElement(
    SysPlainTomlEditor,
    {
      title: "hive",
      sub: "config/hive.toml: the console as the hive's queen",
      name: "hive",
      file: cfg.hive,
      fields: SYS_HIVE_FIELDS,
      reload: load
    }
  ), /* @__PURE__ */ React.createElement(SysPeersEditor, { file: cfg.peers, reload: load }), /* @__PURE__ */ React.createElement(SysOutboxPanel, null), /* @__PURE__ */ React.createElement(
    SysTextFileEditor,
    {
      title: "outbound filter",
      sub: "config/outbound-filter.json",
      file: cfg.outbound_filter,
      route: "/api/system/outbound-filter",
      reload: load,
      hint: '{"terms": [...], "protected": [...], "trusted_peers": [...], "surfaces": {"<surface>": {"add": [...]}}}'
    }
  ), /* @__PURE__ */ React.createElement(
    SysTextFileEditor,
    {
      title: "law",
      sub: "config/law.md: every cousin's boot packet",
      file: cfg.law,
      route: "/api/system/law",
      reload: load,
      rows: 18
    }
  ), /* @__PURE__ */ React.createElement(SysAllowlistEditor, { file: cfg.allowlist, reload: load }), /* @__PURE__ */ React.createElement(SysCommandsView, { commands: cfg.commands }));
}
const SYSTEM_TABS = [
  ["services", "supervisor"],
  ["schedules", "schedules"],
  ["users", "users"],
  ["backup", "backup"],
  ["agent", "agent defaults"],
  ["config", "install config"]
];
const SYS_TAB_KEY = "console_system_tab";
function SystemView({ cousins }) {
  const [tab, setTab] = React.useState(() => {
    try {
      return localStorage.getItem(SYS_TAB_KEY) || "services";
    } catch (_e) {
      return "services";
    }
  });
  const pick = (t) => {
    setTab(t);
    try {
      localStorage.setItem(SYS_TAB_KEY, t);
    } catch (_e) {
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad system-view", style: { maxWidth: 1100 } }, /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, SYSTEM_TABS.map(([id, label]) => /* @__PURE__ */ React.createElement("button", { key: id, className: tab === id ? "sel" : "", onClick: () => pick(id) }, label))), tab === "services" && /* @__PURE__ */ React.createElement(SysSupervisorPanel, null), tab === "schedules" && /* @__PURE__ */ React.createElement(SchedulesPanel, { cousins }), tab === "users" && /* @__PURE__ */ React.createElement(SysUsersPanel, null), tab === "backup" && /* @__PURE__ */ React.createElement(SysBackupPanel, { cousins }), tab === "agent" && /* @__PURE__ */ React.createElement(SysAgentDefaultsPanel, null), tab === "config" && /* @__PURE__ */ React.createElement(SysConfigEditors, null));
}
registerView({ id: "system", label: "System", icon: I.host, order: 60, render: (props) => /* @__PURE__ */ React.createElement(SystemView, { ...props }) });
registerSlot("inspector.panels", {
  id: "schedules",
  order: 40,
  render: ({ cousin }) => cousin && !cousin.remote && cousin.type !== "remote" ? /* @__PURE__ */ React.createElement(SchedulesPanel, { slug: cousin.slug }) : null
});
Object.assign(window, { SystemView, SchedulesPanel });
