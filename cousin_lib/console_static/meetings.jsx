// meetings.jsx: the Meetings view. A meeting is a turn-taking thread
// between the console user and a set of cousins (docs/meetings.md). The
// store is the library's; the console opens, posts, skips and closes
// through /api/meetings, and cousins speak through cousin-meeting. Both
// write the same store, so this view refetches on every meeting-change
// event (re-dispatched by app.jsx as "fw-meeting-change") and on a 5 s
// fallback poll while the page is visible.

const MEETING_STATE_TONE = { open: "green", closing: "amber", closed: "gray" };

// A cousin that can be invited: a local, running cousin (never a worker).
function meetingIsRemote(c) { return c.type === "remote" || !!c.host; }
function meetingEligible(c) { return c.status === "running" && !meetingIsRemote(c); }

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
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch (_e) { return ""; }
}

// The one-line summary of whose turn it is, shared by the list and the
// thread's banner. `now` is epoch seconds.
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

// Deep link: ?view=meetings&meeting=<id> opens that thread on load (read
// once by app.jsx, like ?cousin=; the address bar is not rewritten).
function MeetingsView({ cousins, sessionUser, initialMeeting }) {
  const [meetings, setMeetings] = React.useState(null);
  const [selectedId, setSelectedId] = React.useState(() => {
    const n = parseInt(initialMeeting || "", 10);
    return isFinite(n) && n > 0 ? n : null;
  });
  const [meeting, setMeeting] = React.useState(null); // the selected one, with its transcript
  const [creating, setCreating] = React.useState(false);
  const [status, setStatus] = React.useState(null);
  const [now, setNow] = React.useState(() => Date.now() / 1000);
  const selectedRef = React.useRef(selectedId);
  selectedRef.current = selectedId;

  const pullList = React.useCallback(async () => {
    const d = await apiGet("/api/meetings");
    if (d) setMeetings(d.meetings || []);
  }, []);

  const pullMeeting = React.useCallback(async (id) => {
    if (!id) { setMeeting(null); return; }
    const d = await apiGet(`/api/meetings/${id}`);
    // A reply for a meeting the user has since left is dropped.
    if (selectedRef.current !== id) return;
    setMeeting(d ? d.meeting || null : null);
  }, []);

  const pullAll = React.useCallback(() => {
    pullList();
    pullMeeting(selectedRef.current);
  }, [pullList, pullMeeting]);

  React.useEffect(() => { pullMeeting(selectedId); }, [selectedId, pullMeeting]);

  // Live: every meeting-change refetches the list and the open thread.
  // The poll is the fallback for a dropped event stream, and runs only
  // while the page is in the foreground.
  React.useEffect(() => {
    pullList();
    const onChange = () => pullAll();
    window.addEventListener("fw-meeting-change", onChange);
    const id = setInterval(() => {
      if (document.visibilityState === "visible") pullAll();
    }, 5000);
    const onVisible = () => { if (document.visibilityState === "visible") pullAll(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("fw-meeting-change", onChange);
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pullList, pullAll]);

  // A one-second clock for the "waiting since" counters.
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);

  React.useEffect(() => {
    if (!status) return;
    const t = setTimeout(() => setStatus(null), 5000);
    return () => clearTimeout(t);
  }, [status]);

  const select = (id) => { setSelectedId(id); setMeeting(null); };

  const act = async (path, body, okMsg) => {
    const { r, d } = await apiSend("POST", path, body);
    if (!r.ok || d.error) {
      setStatus({ ok: false, msg: d.error || `HTTP ${r.status}` });
      return false;
    }
    if (d.meeting) setMeeting(prev => ({ ...(prev || {}), ...d.meeting, transcript: d.meeting.transcript || (prev && prev.transcript) || [] }));
    if (okMsg) setStatus({ ok: true, msg: okMsg });
    pullAll();
    return true;
  };

  const list = meetings || [];
  const selectedRow = list.find(m => m.id === selectedId) || null;
  const current = meeting && meeting.id === selectedId ? meeting : selectedRow;

  return (
    <div className={`mt-layout ${selectedId ? "has-sel" : ""}`}>
      <div className="mt-list">
        <div className="mt-list-hdr">
          <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" }}>
            {meetings === null ? "loading..." : `${list.length} meeting${list.length === 1 ? "" : "s"}`}
          </span>
          <span style={{ flex: 1 }} />
          <button className="btn primary" onClick={() => setCreating(true)}>{I.plus} new meeting</button>
        </div>
        <div className="mt-list-body">
          {list.map(m => (
            <div key={m.id}
                 className={`mt-item ${m.id === selectedId ? "active" : ""} ${m.state === "closed" ? "closed" : ""}`}
                 onClick={() => select(m.id)}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="mt-item-topic">{m.topic || "(no topic)"}</span>
                <span style={{ flex: 1 }} />
                <Pill tone={MEETING_STATE_TONE[m.state] || "gray"}>{m.state}</Pill>
              </div>
              <div className="mt-item-meta">
                #{m.id} · {(m.participants || []).map(s => "@" + s).join(" ")}
              </div>
              <div className="mt-item-meta" style={{ color: m.state === "open" && !m.turn_slug ? "var(--accent)" : undefined }}>
                {m.state === "closed"
                  ? `closed · ${m.entries || 0} entries`
                  : `round ${m.round || 0} · ${m.turn_slug ? `${m.turn_slug}'s turn` : "the floor is yours"}`}
              </div>
            </div>
          ))}
          {meetings !== null && list.length === 0 && (
            <div className="muted" style={{ padding: 20, textAlign: "center", fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" }}>
              no meetings yet
            </div>
          )}
        </div>
      </div>

      <div className="mt-thread">
        {!selectedId && (
          <div className="mt-empty">pick a meeting, or start a new one</div>
        )}
        {selectedId && !current && (
          <div className="mt-empty">{meetings === null ? "loading..." : `meeting #${selectedId} not found`}</div>
        )}
        {selectedId && current && (
          <MeetingThread
            m={current}
            now={now}
            sessionUser={sessionUser}
            status={status}
            onBack={() => select(null)}
            onPost={(text) => act(`/api/meetings/${current.id}/post`, { text })}
            onSkip={() => act(`/api/meetings/${current.id}/skip`, undefined, "speaker skipped")}
            onClose={() => {
              const msg = current.facilitator
                ? `Close meeting #${current.id}? ${current.facilitator} will write the minutes.`
                : `Close meeting #${current.id}? There is no facilitator, so no minutes are written.`;
              if (!window.confirm(msg)) return;
              act(`/api/meetings/${current.id}/close`, undefined, "closing");
            }}
            onDelete={async () => {
              const running = current.state !== "closed";
              if (!window.confirm(`Delete meeting #${current.id} and its whole transcript?`
                  + (running ? " It is still running: the participants are told it is over." : ""))) return;
              const { r, d } = await apiSend("DELETE", `/api/meetings/${current.id}`);
              if (!r.ok || d.error) { setStatus({ ok: false, msg: d.error || `HTTP ${r.status}` }); return; }
              select(null);
              pullAll();
            }}
          />
        )}
      </div>

      {creating && (
        <NewMeetingModal
          cousins={cousins}
          onClose={() => setCreating(false)}
          onCreated={(m) => {
            setCreating(false);
            pullList();
            if (m && m.id) select(m.id);
          }}
        />
      )}
    </div>
  );
}

function MeetingThread({ m, now, sessionUser, status, onBack, onPost, onSkip, onClose, onDelete }) {
  const [text, setText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const scrollRef = React.useRef(null);
  const stickRef = React.useRef(true);
  const transcript = m.transcript || [];
  const floor = m.state === "open" && m.mode === "floor";

  // Follow the tail while the reader is at the bottom; leave them where
  // they are when they scrolled up to read.
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  };
  React.useEffect(() => { stickRef.current = true; }, [m.id]);
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
      if (ok) { setText(""); stickRef.current = true; }
    } finally { setBusy(false); }
  };

  const banner = meetingTurnText(m, now);
  const bannerTone = m.state === "closed" ? "closed"
    : (m.state === "closing" || m.mode === "minutes") ? "closing"
    : floor ? "floor" : "waiting";
  const late = m.turn_slug && m.turn_started && m.turn_timeout_s
    && m.state !== "closed" && now - m.turn_started > m.turn_timeout_s;

  const speakerLabel = (e) => (e.kind === "user" && sessionUser && e.speaker === sessionUser) ? "you" : (e.speaker || "-");

  return (
    <>
      <div className="mt-thread-hdr">
        <button className="btn ghost mt-back" onClick={onBack} title="back to the list">‹ meetings</button>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="mt-thread-topic">{m.topic || "(no topic)"}</div>
          <div className="mt-item-meta">
            #{m.id} · order{" "}
            {(m.participants || []).map((s, i) => (
              <span key={s} className={`mt-order${m.turn_slug === s && m.state !== "closed" ? " now" : ""}`}
                    title={m.turn_slug === s ? "speaking now" : `turn ${i + 1} of every round`}>
                {i + 1} @{s}
              </span>
            ))}
            {m.facilitator ? ` · facilitator @${m.facilitator}` : ""}
          </div>
        </div>
        <Pill tone={MEETING_STATE_TONE[m.state] || "gray"}>{m.state}</Pill>
        <button className="btn ghost danger" onClick={onDelete}
                title="delete this meeting and its transcript">delete</button>
      </div>

      <div className={`mt-banner ${bannerTone}`}>
        <span>{banner}</span>
        {m.turn_slug && m.state !== "closed" && !m.turn_delivered && <span className="mt-banner-note">not delivered yet</span>}
        {late && <span className="mt-banner-note">past the {Math.round(m.turn_timeout_s / 60)} min timeout</span>}
      </div>

      <div className="mt-transcript" ref={scrollRef} onScroll={onScroll}>
        {transcript.length === 0 && (
          <div className="mt-empty" style={{ height: "auto", padding: 20 }}>no entries yet</div>
        )}
        {transcript.map(e => {
          if (e.kind === "pass") {
            return (
              <div key={e.id} className="mt-entry kind-pass">
                <b>{speakerLabel(e)}</b> passed{e.text ? `: ${e.text}` : ""} <span className="mt-entry-time">r{e.round} · {fmtEntryTime(e.created_at)}</span>
              </div>
            );
          }
          if (e.kind === "system") {
            return (
              <div key={e.id} className="mt-entry kind-system">
                {e.text} <span className="mt-entry-time">{fmtEntryTime(e.created_at)}</span>
              </div>
            );
          }
          const md = (e.kind === "minutes" || e.kind === "cousin") && window.renderMarkdown;
          return (
            <div key={e.id} className={`mt-entry kind-${e.kind || "cousin"}`}>
              <div className="mt-entry-head">
                <span className="mt-entry-speaker">{e.kind === "minutes" ? `minutes · ${speakerLabel(e)}` : speakerLabel(e)}</span>
                <span className="mt-entry-time">r{e.round} · {fmtEntryTime(e.created_at)}</span>
              </div>
              {md
                ? <div className="chat-md mt-entry-text" dangerouslySetInnerHTML={{ __html: window.renderMarkdown(e.text || "") }} />
                : <div className="mt-entry-text" style={{ whiteSpace: "pre-wrap" }}>{e.text}</div>}
            </div>
          );
        })}
      </div>

      <div className="mt-composer">
        <textarea
          className="txt"
          value={text}
          disabled={!floor || busy}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          placeholder={floor ? "@slug to ask one participant; plain text starts a round" : "wait for the floor"}
        />
        <div className="mt-composer-row">
          <span className="mt-hint">
            {status
              ? <span style={{ color: status.ok ? "var(--green)" : "var(--red)" }}>{status.msg}</span>
              : "@slug to ask one participant; plain text starts a round"}
          </span>
          <span style={{ flex: 1 }} />
          <button className="btn" disabled={!m.turn_slug || m.state === "closed"} onClick={onSkip}
                  title="skip the current speaker">skip speaker</button>
          <button className="btn danger" disabled={m.state !== "open"} onClick={onClose}
                  title={m.facilitator ? "close; the facilitator writes the minutes" : "close the meeting"}>close</button>
          <button className="btn primary" disabled={!floor || busy || !text.trim()} onClick={send}>
            {busy ? "..." : "send"}
          </button>
        </div>
      </div>
    </>
  );
}

function NewMeetingModal({ cousins, onClose, onCreated }) {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const [topic, setTopic] = React.useState("");
  const [picked, setPicked] = React.useState([]); // slugs in speaking order
  const [facilitator, setFacilitator] = React.useState("");
  const [minutes, setMinutes] = React.useState("10");
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  // The user's sidebar groups (teams), from the same preference the
  // sidebar reads. A cousin with no assignment sits in "sessions".
  const [sidebar, setSidebar] = React.useState(null);
  React.useEffect(() => {
    let alive = true;
    apiGet("/api/prefs/sidebar").then(d => { if (alive && d) setSidebar(d.sidebar || null); });
    return () => { alive = false; };
  }, []);

  const pool = (cousins || []).filter(c => c.type !== "worker" && (showHidden || !c.hidden));
  const eligible = pool.filter(meetingEligible);
  const groups = (sidebar && Array.isArray(sidebar.groups) && sidebar.groups.length)
    ? sidebar.groups : [{ id: "sessions", name: "Sessions" }];
  const assignments = (sidebar && sidebar.assignments) || {};
  const groupIds = new Set(groups.map(g => g.id));
  const groupOf = (slug) => {
    const gid = assignments[slug] || "sessions";
    return groupIds.has(gid) ? gid : groups[0].id;
  };

  const add = (slugs) => setPicked(p => [...p, ...slugs.filter(s => !p.includes(s))]);
  const toggle = (slug) => setPicked(p => p.includes(slug) ? p.filter(s => s !== slug) : [...p, slug]);

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

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>new meeting</span>
          <span style={{ color: "var(--fg-3)", marginLeft: "auto" }}>console / meetings / new</span>
        </div>
        <div className="body">
          <FormField label="topic" hint="What the meeting is for. Every participant sees it on each turn.">
            <input className="txt" autoFocus value={topic} onChange={e => setTopic(e.target.value)} placeholder="review the release plan" />
          </FormField>
          <div style={{ marginTop: 14 }} />
          <FormField label={`participants · ${picked.length} picked`} hint="Speaking order is the order you pick them in.">
            <div className="mt-pick-row">
              {groups.map(g => {
                const members = eligible.filter(c => groupOf(c.slug) === g.id).map(c => c.slug);
                return (
                  <button key={g.id} type="button" className="btn" disabled={!members.length}
                          onClick={() => add(members)}
                          title={members.length ? `add the running members of ${g.name}` : `no running local cousin in ${g.name}`}>
                    {I.plus} {String(g.name || g.id).toLowerCase()} <span style={{ color: "var(--fg-3)" }}>{members.length}</span>
                  </button>
                );
              })}
              <button type="button" className="btn" disabled={!eligible.length}
                      onClick={() => add(eligible.map(c => c.slug))}>{I.plus} all running</button>
              {picked.length > 0 && <button type="button" className="btn ghost" onClick={() => setPicked([])}>clear</button>}
            </div>
            <div className="mt-pick-list">
              {pool.map(c => {
                const remote = meetingIsRemote(c);
                const ok = meetingEligible(c);
                const idx = picked.indexOf(c.slug);
                return (
                  <label key={c.slug} className={`mt-pick ${ok ? "" : "disabled"}`}
                         title={remote ? "remote cousins cannot join yet" : ok ? "" : "start it first"}>
                    <input type="checkbox" disabled={!ok} checked={idx >= 0} onChange={() => toggle(c.slug)} />
                    <Led state={c.status === "running" ? "running" : "stopped"} />
                    <span>{c.slug}</span>
                    {idx >= 0 && <span className="mt-pick-order">{idx + 1}</span>}
                    {!ok && <span className="mt-pick-hint">{remote ? "remote cousins cannot join yet" : "start it first"}</span>}
                  </label>
                );
              })}
              {pool.length === 0 && <div className="mt-pick-hint" style={{ padding: 6 }}>no cousins</div>}
            </div>
          </FormField>
          <div style={{ marginTop: 14 }} />
          <div className="grid2">
            <FormField label="facilitator (optional)" hint="Writes the minutes when the meeting closes.">
              <select className="sel" value={facilitator} onChange={e => setFacilitator(e.target.value)}>
                <option value="">none</option>
                {eligible.map(c => <option key={c.slug} value={c.slug}>{c.slug}</option>)}
              </select>
            </FormField>
            <FormField label="turn timeout · minutes" hint="A speaker who stays silent this long is skipped.">
              <input className="txt" type="number" min="1" value={minutes} onChange={e => setMinutes(e.target.value)} />
            </FormField>
          </div>
        </div>
        {error && (
          <div style={{ padding: "8px 16px", color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11, borderTop: "1px solid var(--line)" }}>
            · {error}
          </div>
        )}
        <div className="foot">
          <button className="btn ghost" onClick={onClose}>cancel</button>
          <button className="btn primary" disabled={!valid || busy} onClick={submit}>
            {I.plus} {busy ? "opening..." : "open meeting"}
          </button>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { MeetingsView, MeetingThread, NewMeetingModal, meetingTurnText });
