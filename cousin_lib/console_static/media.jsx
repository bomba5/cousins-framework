// Chat media: how an attachment renders inside a bubble.
// docs/console-spec.md, "The chat media viewer". Loaded before chat.jsx,
// which reads these names off window.

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

// One attachment inside a bubble, by kind.
function InlineMedia({ media, onOpen }) {
  if (!media) return null;
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

Object.assign(window, {
  mediaKindFromUrl, normalizeMediaKind, attachmentMedia,
  LoopingPreview, InlineMedia,
});
