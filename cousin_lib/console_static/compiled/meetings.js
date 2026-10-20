const MEETING_STATE_TONE = { open: "green", closing: "amber", closed: "gray" };
function meetingIsRemote(c) {
  return c.type === "remote" || !!c.host;
}
function meetingEligible(c) {
  return c.status === "running" && !meetingIsRemote(c);
}
function fmtMmSs(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
}
function fmtEntryTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString(void 0, { hour: "2-digit", minute: "2-digit" });
  } catch (_e) {
    return "";
  }
}
function meetingTurnText(m, now) {
  if (!m) return "";
  if (m.state === "closed") return "Closed";
  if (m.state === "closing" || m.mode === "minutes") {
    return `Closing: ${m.facilitator || m.turn_slug || "the facilitator"} is writing the minutes`;
  }
  if (!m.turn_slug || m.mode === "floor") return "The floor is yours";
  const since = m.turn_started ? ` (waiting since ${fmtMmSs(now - m.turn_started)})` : "";
  if (m.mode === "direct") return `Direct question to ${m.turn_slug}${since}`;
  return `Round ${m.round || 1}: ${m.turn_slug} is speaking${since}`;
}
function MeetingsView({ cousins, sessionUser, initialMeeting }) {
  const [meetings, setMeetings] = React.useState(null);
  const [selectedId, setSelectedId] = React.useState(() => {
    const n = parseInt(initialMeeting || "", 10);
    return isFinite(n) && n > 0 ? n : null;
  });
  const [meeting, setMeeting] = React.useState(null);
  const [creating, setCreating] = React.useState(false);
  const [status, setStatus] = React.useState(null);
  const [now, setNow] = React.useState(() => Date.now() / 1e3);
  const selectedRef = React.useRef(selectedId);
  selectedRef.current = selectedId;
  const pullList = React.useCallback(async () => {
    const d = await apiGet("/api/meetings");
    if (d) setMeetings(d.meetings || []);
  }, []);
  const pullMeeting = React.useCallback(async (id) => {
    if (!id) {
      setMeeting(null);
      return;
    }
    const d = await apiGet(`/api/meetings/${id}`);
    if (selectedRef.current !== id) return;
    setMeeting(d ? d.meeting || null : null);
  }, []);
  const pullAll = React.useCallback(() => {
    pullList();
    pullMeeting(selectedRef.current);
  }, [pullList, pullMeeting]);
  React.useEffect(() => {
    pullMeeting(selectedId);
  }, [selectedId, pullMeeting]);
  React.useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (selectedId) p.set("meeting", String(selectedId));
    else p.delete("meeting");
    const next = "?" + p.toString();
    if (next !== window.location.search) {
      window.history.replaceState(
        window.history.state,
        "",
        window.location.pathname + next + window.location.hash
      );
    }
  }, [selectedId]);
  React.useEffect(() => {
    pullList();
    const onChange = () => pullAll();
    window.addEventListener("fw-meeting-change", onChange);
    const id = setInterval(() => {
      if (document.visibilityState === "visible") pullAll();
    }, 5e3);
    const onVisible = () => {
      if (document.visibilityState === "visible") pullAll();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("fw-meeting-change", onChange);
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pullList, pullAll]);
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1e3), 1e3);
    return () => clearInterval(id);
  }, []);
  React.useEffect(() => {
    if (!status) return;
    const t = setTimeout(() => setStatus(null), 5e3);
    return () => clearTimeout(t);
  }, [status]);
  const select = (id) => {
    setSelectedId(id);
    setMeeting(null);
  };
  const act = async (path, body, okMsg) => {
    const { r, d } = await apiSend("POST", path, body);
    if (!r.ok || d.error) {
      setStatus({ ok: false, msg: d.error || `HTTP ${r.status}` });
      return false;
    }
    if (d.meeting) setMeeting((prev) => ({ ...prev || {}, ...d.meeting, transcript: d.meeting.transcript || prev && prev.transcript || [] }));
    if (okMsg) setStatus({ ok: true, msg: okMsg });
    pullAll();
    return true;
  };
  const list = meetings || [];
  const selectedRow = list.find((m) => m.id === selectedId) || null;
  const current = meeting && meeting.id === selectedId ? meeting : selectedRow;
  return /* @__PURE__ */ React.createElement("div", { className: `mt-layout ${selectedId ? "has-sel" : ""}` }, /* @__PURE__ */ React.createElement("div", { className: "mt-list" }, /* @__PURE__ */ React.createElement("div", { className: "mt-list-hdr" }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" } }, meetings === null ? "loading..." : `${list.length} meeting${list.length === 1 ? "" : "s"}`), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: () => setCreating(true) }, I.plus, " new meeting")), /* @__PURE__ */ React.createElement("div", { className: "mt-list-body" }, list.map((m) => /* @__PURE__ */ React.createElement(
    "div",
    {
      key: m.id,
      className: `mt-item ${m.id === selectedId ? "active" : ""} ${m.state === "closed" ? "closed" : ""}`,
      onClick: () => select(m.id)
    },
    /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 6 } }, /* @__PURE__ */ React.createElement("span", { className: "mt-item-topic" }, m.topic || "(no topic)"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement(Pill, { tone: MEETING_STATE_TONE[m.state] || "gray" }, m.state)),
    /* @__PURE__ */ React.createElement("div", { className: "mt-item-meta" }, "#", m.id, " · ", (m.participants || []).map((s) => "@" + s).join(" ")),
    /* @__PURE__ */ React.createElement("div", { className: "mt-item-meta", style: { color: m.state === "open" && !m.turn_slug ? "var(--accent)" : void 0 } }, m.state === "closed" ? `closed · ${m.entries || 0} entries` : `round ${m.round || 0} · ${m.turn_slug ? `${m.turn_slug}'s turn` : "the floor is yours"}`)
  )), meetings !== null && list.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "muted", style: { padding: 20, textAlign: "center", fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" } }, "no meetings yet"))), /* @__PURE__ */ React.createElement("div", { className: "mt-thread" }, !selectedId && /* @__PURE__ */ React.createElement("div", { className: "mt-empty" }, "pick a meeting, or start a new one"), selectedId && !current && /* @__PURE__ */ React.createElement("div", { className: "mt-empty" }, meetings === null ? "loading..." : `meeting #${selectedId} not found`), selectedId && current && /* @__PURE__ */ React.createElement(
    MeetingThread,
    {
      m: current,
      now,
      sessionUser,
      status,
      onBack: () => select(null),
      onPost: (text) => act(`/api/meetings/${current.id}/post`, { text }),
      onSkip: () => act(`/api/meetings/${current.id}/skip`, void 0, "speaker skipped"),
      onClose: () => {
        const msg = current.facilitator ? `Close meeting #${current.id}? ${current.facilitator} will write the minutes.` : `Close meeting #${current.id}? There is no facilitator, so no minutes are written.`;
        if (!window.confirm(msg)) return;
        act(`/api/meetings/${current.id}/close`, void 0, "closing");
      },
      onDelete: async () => {
        const running = current.state !== "closed";
        if (!window.confirm(`Delete meeting #${current.id} and its whole transcript?` + (running ? " It is still running: the participants are told it is over." : ""))) return;
        const { r, d } = await apiSend("DELETE", `/api/meetings/${current.id}`);
        if (!r.ok || d.error) {
          setStatus({ ok: false, msg: d.error || `HTTP ${r.status}` });
          return;
        }
        select(null);
        pullAll();
      }
    }
  )), creating && /* @__PURE__ */ React.createElement(
    NewMeetingModal,
    {
      cousins,
      onClose: () => setCreating(false),
      onCreated: (m) => {
        setCreating(false);
        pullList();
        if (m && m.id) select(m.id);
      }
    }
  ));
}
function MeetingThread({ m, now, sessionUser, status, onBack, onPost, onSkip, onClose, onDelete }) {
  const [text, setText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const scrollRef = React.useRef(null);
  const stickRef = React.useRef(true);
  const transcript = m.transcript || [];
  const floor = m.state === "open" && m.mode === "floor";
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  };
  React.useEffect(() => {
    stickRef.current = true;
  }, [m.id]);
  React.useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [m.id, transcript.length]);
  const send = async () => {
    const body = text.trim();
    if (!body || !floor || busy) return;
    setBusy(true);
    try {
      const ok = await onPost(body);
      if (ok) {
        setText("");
        stickRef.current = true;
      }
    } finally {
      setBusy(false);
    }
  };
  const banner = meetingTurnText(m, now);
  const bannerTone = m.state === "closed" ? "closed" : m.state === "closing" || m.mode === "minutes" ? "closing" : floor ? "floor" : "waiting";
  const late = m.turn_slug && m.turn_started && m.turn_timeout_s && m.state !== "closed" && now - m.turn_started > m.turn_timeout_s;
  const speakerLabel = (e) => e.kind === "user" && sessionUser && e.speaker === sessionUser ? "you" : e.speaker || "-";
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "mt-thread-hdr" }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost mt-back", onClick: onBack, title: "back to the list" }, "‹ meetings"), /* @__PURE__ */ React.createElement("div", { style: { minWidth: 0, flex: 1 } }, /* @__PURE__ */ React.createElement("div", { className: "mt-thread-topic" }, m.topic || "(no topic)"), /* @__PURE__ */ React.createElement("div", { className: "mt-item-meta" }, "#", m.id, " · order", " ", (m.participants || []).map((s, i) => /* @__PURE__ */ React.createElement(
    "span",
    {
      key: s,
      className: `mt-order${m.turn_slug === s && m.state !== "closed" ? " now" : ""}`,
      title: m.turn_slug === s ? "speaking now" : `turn ${i + 1} of every round`
    },
    i + 1,
    " @",
    s
  )), m.facilitator ? ` · facilitator @${m.facilitator}` : "")), /* @__PURE__ */ React.createElement(Pill, { tone: MEETING_STATE_TONE[m.state] || "gray" }, m.state), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost danger",
      onClick: onDelete,
      title: "delete this meeting and its transcript"
    },
    "delete"
  )), /* @__PURE__ */ React.createElement("div", { className: `mt-banner ${bannerTone}` }, /* @__PURE__ */ React.createElement("span", null, banner), m.turn_slug && m.state !== "closed" && !m.turn_delivered && /* @__PURE__ */ React.createElement("span", { className: "mt-banner-note" }, "not delivered yet"), late && /* @__PURE__ */ React.createElement("span", { className: "mt-banner-note" }, "past the ", Math.round(m.turn_timeout_s / 60), " min timeout")), /* @__PURE__ */ React.createElement("div", { className: "mt-transcript", ref: scrollRef, onScroll }, transcript.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mt-empty", style: { height: "auto", padding: 20 } }, "no entries yet"), transcript.map((e) => {
    if (e.kind === "pass") {
      return /* @__PURE__ */ React.createElement("div", { key: e.id, className: "mt-entry kind-pass" }, /* @__PURE__ */ React.createElement("b", null, speakerLabel(e)), " passed", e.text ? `: ${e.text}` : "", " ", /* @__PURE__ */ React.createElement("span", { className: "mt-entry-time" }, "r", e.round, " · ", fmtEntryTime(e.created_at)));
    }
    if (e.kind === "system") {
      return /* @__PURE__ */ React.createElement("div", { key: e.id, className: "mt-entry kind-system" }, e.text, " ", /* @__PURE__ */ React.createElement("span", { className: "mt-entry-time" }, fmtEntryTime(e.created_at)));
    }
    const md = (e.kind === "minutes" || e.kind === "cousin") && window.renderMarkdown;
    return /* @__PURE__ */ React.createElement("div", { key: e.id, className: `mt-entry kind-${e.kind || "cousin"}` }, /* @__PURE__ */ React.createElement("div", { className: "mt-entry-head" }, /* @__PURE__ */ React.createElement("span", { className: "mt-entry-speaker" }, e.kind === "minutes" ? `minutes · ${speakerLabel(e)}` : speakerLabel(e)), /* @__PURE__ */ React.createElement("span", { className: "mt-entry-time" }, "r", e.round, " · ", fmtEntryTime(e.created_at))), md ? /* @__PURE__ */ React.createElement("div", { className: "chat-md mt-entry-text", dangerouslySetInnerHTML: { __html: window.renderMarkdown(e.text || "") } }) : /* @__PURE__ */ React.createElement("div", { className: "mt-entry-text", style: { whiteSpace: "pre-wrap" } }, e.text));
  })), /* @__PURE__ */ React.createElement("div", { className: "mt-composer" }, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt",
      value: text,
      disabled: !floor || busy,
      onChange: (e) => setText(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          send();
        }
      },
      placeholder: floor ? "@slug to ask one participant; plain text starts a round" : "wait for the floor"
    }
  ), /* @__PURE__ */ React.createElement("div", { className: "mt-composer-row" }, /* @__PURE__ */ React.createElement("span", { className: "mt-hint" }, status ? /* @__PURE__ */ React.createElement("span", { style: { color: status.ok ? "var(--green)" : "var(--red)" } }, status.msg) : "@slug to ask one participant; plain text starts a round"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      disabled: !m.turn_slug || m.state === "closed",
      onClick: onSkip,
      title: "skip the current speaker"
    },
    "skip speaker"
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn danger",
      disabled: m.state !== "open",
      onClick: onClose,
      title: m.facilitator ? "close; the facilitator writes the minutes" : "close the meeting"
    },
    "close"
  ), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !floor || busy || !text.trim(), onClick: send }, busy ? "..." : "send"))));
}
function NewMeetingModal({ cousins, onClose, onCreated }) {
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const [topic, setTopic] = React.useState("");
  const [picked, setPicked] = React.useState([]);
  const [facilitator, setFacilitator] = React.useState("");
  const [minutes, setMinutes] = React.useState("10");
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [sidebar, setSidebar] = React.useState(null);
  React.useEffect(() => {
    let alive = true;
    apiGet("/api/prefs/sidebar").then((d) => {
      if (alive && d) setSidebar(d.sidebar || null);
    });
    return () => {
      alive = false;
    };
  }, []);
  const pool = (cousins || []).filter((c) => c.type !== "worker" && (showHidden || !c.hidden));
  const eligible = pool.filter(meetingEligible);
  const groups = sidebar && Array.isArray(sidebar.groups) && sidebar.groups.length ? sidebar.groups : [{ id: "sessions", name: "Sessions" }];
  const assignments = sidebar && sidebar.assignments || {};
  const groupIds = new Set(groups.map((g) => g.id));
  const groupOf = (slug) => {
    const gid = assignments[slug] || "sessions";
    return groupIds.has(gid) ? gid : groups[0].id;
  };
  const add = (slugs) => setPicked((p) => [...p, ...slugs.filter((s) => !p.includes(s))]);
  const toggle = (slug) => setPicked((p) => p.includes(slug) ? p.filter((s) => s !== slug) : [...p, slug]);
  const mins = parseInt(minutes, 10);
  const valid = topic.trim() && picked.length > 0 && isFinite(mins) && mins >= 1;
  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = { topic: topic.trim(), participants: picked, timeout_s: Math.max(60, mins * 60) };
      if (facilitator) body.facilitator = facilitator;
      const { r, d } = await apiSend("POST", "/api/meetings", body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      onCreated(d.meeting);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "new meeting"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", marginLeft: "auto" } }, "console / meetings / new")), /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement(FormField, { label: "topic", hint: "What the meeting is for. Every participant sees it on each turn." }, /* @__PURE__ */ React.createElement("input", { className: "txt", autoFocus: true, value: topic, onChange: (e) => setTopic(e.target.value), placeholder: "review the release plan" })), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement(FormField, { label: `participants · ${picked.length} picked`, hint: "Speaking order is the order you pick them in." }, /* @__PURE__ */ React.createElement("div", { className: "mt-pick-row" }, groups.map((g) => {
    const members = eligible.filter((c) => groupOf(c.slug) === g.id).map((c) => c.slug);
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        key: g.id,
        type: "button",
        className: "btn",
        disabled: !members.length,
        onClick: () => add(members),
        title: members.length ? `add the running members of ${g.name}` : `no running local cousin in ${g.name}`
      },
      I.plus,
      " ",
      String(g.name || g.id).toLowerCase(),
      " ",
      /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, members.length)
    );
  }), /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: "btn",
      disabled: !eligible.length,
      onClick: () => add(eligible.map((c) => c.slug))
    },
    I.plus,
    " all running"
  ), picked.length > 0 && /* @__PURE__ */ React.createElement("button", { type: "button", className: "btn ghost", onClick: () => setPicked([]) }, "clear")), /* @__PURE__ */ React.createElement("div", { className: "mt-pick-list" }, pool.map((c) => {
    const remote = meetingIsRemote(c);
    const ok = meetingEligible(c);
    const idx = picked.indexOf(c.slug);
    return /* @__PURE__ */ React.createElement(
      "label",
      {
        key: c.slug,
        className: `mt-pick ${ok ? "" : "disabled"}`,
        title: remote ? "remote cousins cannot join yet" : ok ? "" : "start it first"
      },
      /* @__PURE__ */ React.createElement("input", { type: "checkbox", disabled: !ok, checked: idx >= 0, onChange: () => toggle(c.slug) }),
      /* @__PURE__ */ React.createElement(Led, { state: c.status === "running" ? "running" : "stopped" }),
      /* @__PURE__ */ React.createElement("span", null, c.slug),
      idx >= 0 && /* @__PURE__ */ React.createElement("span", { className: "mt-pick-order" }, idx + 1),
      !ok && /* @__PURE__ */ React.createElement("span", { className: "mt-pick-hint" }, remote ? "remote cousins cannot join yet" : "start it first")
    );
  }), pool.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "mt-pick-hint", style: { padding: 6 } }, "no cousins"))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "facilitator (optional)", hint: "Writes the minutes when the meeting closes." }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: facilitator, onChange: (e) => setFacilitator(e.target.value) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "none"), eligible.map((c) => /* @__PURE__ */ React.createElement("option", { key: c.slug, value: c.slug }, c.slug)))), /* @__PURE__ */ React.createElement(FormField, { label: "turn timeout · minutes", hint: "A speaker who stays silent this long is skipped." }, /* @__PURE__ */ React.createElement("input", { className: "txt", type: "number", min: "1", value: minutes, onChange: (e) => setMinutes(e.target.value) })))), error && /* @__PURE__ */ React.createElement("div", { style: { padding: "8px 16px", color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11, borderTop: "1px solid var(--line)" } }, "· ", error), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: onClose }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !valid || busy, onClick: submit }, I.plus, " ", busy ? "opening..." : "open meeting"))));
}
Object.assign(window, { MeetingsView, MeetingThread, NewMeetingModal, meetingTurnText });
