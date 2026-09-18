// Chat media: how an attachment renders inside a bubble, the media on/off
// preference, and the full-size viewer with prev/next across the thread.
// docs/console-spec.md, "The chat media viewer". Loaded before chat.jsx,
// which reads these names off window.

// The media on/off choice is a browser preference, never server state.
// "0" hides every attachment; absent (the default) or anything else shows.
const MEDIA_PREF_KEY = "fw_chat_media";

function readMediaShown() {
  try { return localStorage.getItem(MEDIA_PREF_KEY) !== "0"; }
  catch (e) { return true; }
}

function writeMediaShown(on) {
  try { localStorage.setItem(MEDIA_PREF_KEY, on ? "1" : "0"); }
  catch (e) { /* private window or blocked storage: the toggle still works for this page */ }
}

const _MEDIA_EXT = {
  image: ["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "svg"],
  video: ["mp4", "webm", "mov", "m4v", "ogv"],
  audio: ["mp3", "ogg", "oga", "opus", "wav", "m4a", "aac", "flac"],
};

// The display kind from a URL: a data: URI by its MIME type, anything
// else by the extension of its last path segment. null when unknown.
function mediaKindFromUrl(url) {
  if (typeof url !== "string" || !url) return null;
  const data = /^data:(image|video|audio)\//i.exec(url);
  if (data) return data[1].toLowerCase();
  const clean = url.split(/[?#]/)[0];
  const m = /\.([A-Za-z0-9]+)$/.exec(clean.split("/").pop() || "");
  if (!m) return null;
  const ext = m[1].toLowerCase();
  for (const kind of Object.keys(_MEDIA_EXT)) {
    if (_MEDIA_EXT[kind].includes(ext)) return kind;
  }
  return null;
}

// The stored vocabulary ('image' | 'voice' | 'video') and the console's
// display one ('image' | 'video' | 'audio') both map here.
function normalizeMediaKind(kind) {
  const k = String(kind || "").toLowerCase();
  if (k === "image" || k === "video") return k;
  if (k === "audio" || k === "voice") return "audio";
  return null;
}

// {src, kind} for a message's attachment, or null. The console projects
// an attachment as {url, kind}; the kind the API gives wins, then the
// row's attachment_kind, then the extension. A data: image on a row (a
// client-side echo) is accepted too.
function attachmentMedia(msg) {
  if (!msg) return null;
  let src = null;
  if (msg.attachment && typeof msg.attachment.url === "string") src = msg.attachment.url;
  else if (typeof msg.image === "string" && msg.image.startsWith("data:image/")) src = msg.image;
  if (!src) return null;
  const kind = normalizeMediaKind(msg.attachment && msg.attachment.kind)
            || normalizeMediaKind(msg.attachment_kind)
            || mediaKindFromUrl(src);
  return kind ? { src, kind } : null;
}

// The viewer's gallery: every image and video in the thread, in order.
// Audio plays inline and is not part of it.
function threadMedia(messages) {
  const out = [];
  for (const m of messages || []) {
    const media = attachmentMedia(m);
    if (media && (media.kind === "image" || media.kind === "video")) {
      out.push({ id: m.id, src: media.src, kind: media.kind });
    }
  }
  return out;
}

// Inline video preview: silent and looping, but it plays only while on
// screen. Every clip in a long thread decoding at once is what leaves
// mobile browsers showing black boxes, so an IntersectionObserver plays
// the visible ones and pauses the rest. Without the observer it falls
// back to plain autoplay.
function LoopingPreview({ src, onOpen }) {
  const ref = React.useRef(null);
  const canObserve = typeof IntersectionObserver !== "undefined";
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.muted = true;   // the attribute alone is not always reflected
    if (!canObserve) return;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { const p = el.play && el.play(); if (p && p.catch) p.catch(() => {}); }
        else if (el.pause) el.pause();
      }
    }, { threshold: 0.25 });
    io.observe(el);
    return () => io.disconnect();
  }, [src, canObserve]);
  return (
    <video ref={ref} src={src} className="chat-media chat-media-video"
           muted loop playsInline preload="metadata" autoPlay={!canObserve}
           title="click to open with sound"
           onClick={(e) => { e.preventDefault(); if (onOpen) onOpen(); }} />
  );
}

// One attachment inside a bubble, by kind; a one-line placeholder when
// media is off so the text around it stays readable.
function InlineMedia({ media, shown, onOpen }) {
  if (!media) return null;
  if (!shown) {
    return <div className="chat-media-hidden">[{media.kind} hidden]</div>;
  }
  if (media.kind === "image") {
    return (
      <img src={media.src} alt="attachment" className="chat-media chat-media-image"
           loading="lazy" title="click to open"
           onClick={() => onOpen && onOpen()} />
    );
  }
  if (media.kind === "video") {
    return <LoopingPreview src={media.src} onOpen={onOpen} />;
  }
  return <audio src={media.src} controls preload="metadata" className="chat-media chat-media-audio" />;
}

// The full-size viewer. `items` is the gallery snapshot taken when it
// opened (polling does not reshuffle it under the reader), `start` the
// index clicked. Escape or a click outside the media closes it; the
// arrow keys and the side buttons move through the gallery.
function MediaViewer({ items, start, onClose }) {
  const list = items || [];
  const [index, setIndex] = React.useState(start || 0);
  React.useEffect(() => { setIndex(start || 0); }, [start, items]);
  const count = list.length;
  const go = React.useCallback((step) => {
    if (count < 2) return;
    setIndex(i => (i + step + count) % count);
  }, [count]);

  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); go(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); go(1); }
    };
    window.addEventListener("keydown", onKey, true);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = prevOverflow;
    };
  }, [go, onClose]);

  // Touch: a horizontal swipe moves through the gallery.
  const touchRef = React.useRef(null);
  const onTouchStart = (e) => {
    const t = e.touches && e.touches[0];
    touchRef.current = t ? { x: t.clientX, y: t.clientY } : null;
  };
  const onTouchEnd = (e) => {
    const s = touchRef.current;
    touchRef.current = null;
    const t = e.changedTouches && e.changedTouches[0];
    if (!s || !t) return;
    const dx = t.clientX - s.x, dy = t.clientY - s.y;
    if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) go(dx < 0 ? 1 : -1);
  };

  const current = list[index];
  // Start the full video with sound once per item. A ref callback would
  // run again on every parent render (the chat polls) and restart a
  // video the reader paused.
  const videoRef = React.useRef(null);
  const currentSrc = current ? current.src : null;
  React.useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    el.muted = false;
    const p = el.play && el.play();
    if (p && p.catch) p.catch(() => {});
  }, [currentSrc]);
  if (!current) return null;
  const stop = (e) => e.stopPropagation();

  const body = (
    <div className="media-viewer" role="dialog" aria-modal="true" aria-label="media viewer"
         onClick={onClose} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <div className="media-viewer-stage">
        {current.kind === "image" ? (
          <img key={current.src} src={current.src} alt="" className="media-viewer-item" onClick={stop} />
        ) : (
          // Opened by a click, so sound is allowed on desktop; if the
          // browser still refuses, the controls are there to press play.
          <video key={current.src} ref={videoRef} src={current.src} className="media-viewer-item"
                 controls autoPlay loop playsInline onClick={stop} />
        )}
      </div>
      {count > 1 && (
        <button type="button" className="media-viewer-nav media-viewer-prev" title="previous (left arrow)"
                aria-label="previous" onClick={(e) => { stop(e); go(-1); }}>&lsaquo;</button>
      )}
      {count > 1 && (
        <button type="button" className="media-viewer-nav media-viewer-next" title="next (right arrow)"
                aria-label="next" onClick={(e) => { stop(e); go(1); }}>&rsaquo;</button>
      )}
      <button type="button" className="media-viewer-close" title="close (Esc)" aria-label="close"
              onClick={(e) => { stop(e); onClose(); }}>&times;</button>
      <div className="media-viewer-bar" onClick={stop}>
        <span className="media-viewer-count">{index + 1} / {count}</span>
        <a className="media-viewer-link" href={current.src} target="_blank" rel="noopener noreferrer">open original</a>
        <a className="media-viewer-link" href={current.src} download>download</a>
      </div>
    </div>
  );
  // Portal to <body>: a fixed overlay inside the chat column would be
  // clipped by any ancestor that grows a transform.
  return (window.ReactDOM && ReactDOM.createPortal)
    ? ReactDOM.createPortal(body, document.body)
    : body;
}

Object.assign(window, {
  MEDIA_PREF_KEY, readMediaShown, writeMediaShown,
  mediaKindFromUrl, normalizeMediaKind, attachmentMedia, threadMedia,
  LoopingPreview, InlineMedia, MediaViewer,
});
