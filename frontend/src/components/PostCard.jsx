// One collected post — X, Instagram or Facebook — with the Collector's own
// chrome: platform badge, lag badge, content label, watchlist attribution,
// media thumbnails.
import React from "react";
import { fmtAgo, fmtLag, fmtN, fmtPosted } from "../api/client.js";

const PFP_COLORS = ["#2a6b46", "#a8552e", "#7a4a9e", "#3f6b8f", "#8a6100", "#54303f"];
const pfpColor = (s) => {
  let h = 0;
  for (const c of String(s || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PFP_COLORS[h % PFP_COLORS.length];
};

// ---------------------------------------------------------------------------
// Facebook media: why it is a frame, and how we know before we ask
//
// Facebook SIGNS every fbcdn image URL and writes the expiry into the URL
// itself — `oe=<hex epoch>`, about a week out. Past that moment the identical
// URL answers "URL signature expired", which is why saved Facebook posts here
// showed empty media boxes: the link in the database had died. No <img>, no
// proxy and no cache-buster can revive it — only Facebook can mint a new one.
//
// We store no bytes, so the durable thing we hold is the PERMALINK. Facebook's
// own post embed renders that permalink live and mints fresh image URLs on
// every view, so an expired post is shown by framing the post itself.
//
// Reading `oe` (rather than waiting for onError) is what keeps this quiet: the
// expiry is IN the link, so we know a thumbnail is dead before we request it
// and the operator never sees a broken image flash. onError stays as the
// backstop for links that die for some other reason.
const fbExpiryMs = (u) => {
  const m = /[?&]oe=([0-9A-Fa-f]+)/.exec(String(u || ""));
  if (!m) return null;
  const secs = parseInt(m[1], 16);
  return Number.isFinite(secs) ? secs * 1000 : null;
};
// Unsigned links (no oe=) are NOT assumed dead — absence of an expiry is not
// an expiry.
const fbLinkDead = (u) => {
  const exp = fbExpiryMs(u);
  return exp == null ? false : exp < Date.now();
};
// The stored permalink carries Facebook's click-tracking payload
// (__cft__[0]=..., __tn__=...). The embed plugin wants the bare post URL; the
// story/video ids are the only query keys that identify the post.
const fbEmbedHref = (u) => {
  try {
    const url = new URL(u);
    const keep = new URLSearchParams();
    for (const k of ["story_fbid", "id", "v"]) {
      const v = url.searchParams.get(k);
      if (v) keep.set(k, v);
    }
    const q = keep.toString();
    return `${url.origin}${url.pathname}${q ? `?${q}` : ""}`;
  } catch {
    return String(u || "");
  }
};
const FB_EMBED_W = 348;
const fbEmbedSrc = (u) =>
  "https://www.facebook.com/plugins/post.php?href=" +
  encodeURIComponent(fbEmbedHref(u)) +
  `&show_text=false&width=${FB_EMBED_W}`;

// One framed post. Deliberately lazy: a feed page holds many cards and each
// frame is a real Facebook page load, so a frame is only mounted once its slot
// is near the viewport. Cards the operator never scrolls to cost nothing.
function FbEmbed({ url, tall }) {
  const slot = React.useRef(null);
  const [show, setShow] = React.useState(false);
  React.useEffect(() => {
    if (show) return undefined;
    const el = slot.current;
    if (!el || typeof IntersectionObserver !== "function") { setShow(true); return undefined; }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setShow(true); io.disconnect(); }
    }, { rootMargin: "400px" });
    io.observe(el);
    return () => io.disconnect();
  }, [show]);
  return (
    <div className={`fb-embed${tall ? " tall" : ""}`} ref={slot}
         title="The stored image links for this post have expired — this is the live post, from Facebook">
      {show ? (
        <iframe src={fbEmbedSrc(url)} title="Facebook post" loading="lazy"
                scrolling="no" allowFullScreen
                allow="autoplay; clipboard-write; encrypted-media; picture-in-picture; web-share" />
      ) : (
        <span className="fb-embed-wait">Facebook post</span>
      )}
    </div>
  );
}

function Media({ media, onDead }) {
  if (!media?.length) return null;
  const shown = media.slice(0, 2);
  const extra = media.length - shown.length;
  return (
    <div className="media-col">
      {shown.map((m, i) => (
        <a key={i} className="thumb" href={m.url || m.thumb} target="_blank" rel="noreferrer">
          {(m.thumb || m.url) && (
            <img src={m.thumb || m.url} alt={m.type} loading="lazy"
                 onError={(e) => {
                   if (onDead) onDead();
                   else e.currentTarget.style.display = "none";
                 }} />
          )}
          {m.type !== "photo" && <span className="play" aria-label="video" />}
          {m.type !== "photo" && <span className="kind">{m.type}</span>}
          {m.duration ? <span className="dur">{Math.round(m.duration)}s</span> : null}
          {i === shown.length - 1 && extra > 0 && <span className="more">+{extra}</span>}
        </a>
      ))}
    </div>
  );
}

// Highlight any matched keyword terms inside a plain-text run. Split on the
// terms (one capturing group, so matches land on odd indices) and wrap those
// in <mark> — underlined + tinted via .kw in styles.css, so a keyword-search
// hit is verifiable at a glance.
function highlightTerms(text, terms, keyBase) {
  if (!terms || terms.length === 0) return text;
  // Longest first: regex alternation takes the first branch that matches at a
  // position, so with ["Devendra", "Devendra Fadnavis"] the phrase could never
  // win — half of it would highlight and the rest would look unmatched.
  const esc = [...new Set(terms)]
    .sort((a, b) => String(b).length - String(a).length)
    .map((t) => String(t).trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .filter(Boolean);
  if (esc.length === 0) return text;
  const re = new RegExp(`(${esc.join("|")})`, "gi");
  return String(text).split(re).map((p, j) =>
    p === "" ? null
      : j % 2 === 1
        ? <mark key={`${keyBase}-${j}`} className="kw">{p}</mark>
        : p,
  );
}

// Turn bare URLs into links, and highlight keyword-search terms in the rest.
function withLinks(text, terms) {
  const parts = String(text || "").split(/(https?:\/\/\S+)/g);
  return parts.map((p, i) =>
    /^https?:\/\//.test(p) ? (
      <a key={i} href={p} target="_blank" rel="noreferrer">{p}</a>
    ) : (
      highlightTerms(p, terms, i)
    ),
  );
}

function Pfp({ t, name }) {
  const [broken, setBroken] = React.useState(false);
  if (t.author_avatar && !broken) {
    return (
      <img className="pfp" src={t.author_avatar} alt="" loading="lazy"
           style={{ objectFit: "cover" }} onError={() => setBroken(true)} />
    );
  }
  return (
    <div className="pfp" style={{ background: pfpColor(t.author_username) }}>
      {name.slice(0, 2).toUpperCase()}
    </div>
  );
}

// The content label, as a chip. `cats` is the project's vocabulary so the chip
// can show the human name; without it the key is shown, which is ugly but true
// — better than an empty chip while the vocabulary is still loading.
function LabelChip({ t, cats }) {
  if (!t.label) return null;
  const cat = (cats || []).find((c) => c.key === t.label);
  return (
    <span className={`badge cat cat-${t.label}`}
          title={t.label_source === "human"
            ? "labelled by you" + (t.label_ms ? ` ${fmtAgo(t.label_ms)}` : "")
            : "labelled by Grok" + (t.label_ms ? ` ${fmtAgo(t.label_ms)}` : "")}>
      {cat?.name || t.label}
      {t.label_source === "human" ? " ✎" : ""}
    </span>
  );
}

// Re-label by hand. Writes source='human', which is what stops the next
// classify run overwriting the correction.
function LabelPicker({ t, cats, onLabel }) {
  const [busy, setBusy] = React.useState(false);
  const change = async (key) => {
    setBusy(true);
    try {
      await onLabel(t, key);
    } finally {
      setBusy(false);
    }
  };
  return (
    <select className="lbl-pick" value={t.label || ""} disabled={busy}
            aria-label="Change this post's label"
            onChange={(e) => change(e.target.value)}>
      <option value="">{t.label ? "— clear label —" : "not classified"}</option>
      {(cats || []).map((c) => (
        <option key={c.key} value={c.key}>{c.name}</option>
      ))}
    </select>
  );
}

export default function PostCard({ t, onPin, onUnpin, terms, cats, onLabel }) {
  const name = t.author_display_name || t.author_username || "unknown";
  const media = t.media || [];
  // A stored link that died some other way still flips the card to the frame.
  const [imgDead, setImgDead] = React.useState(false);
  const isFb = t.platform === "facebook" && !!t.url;
  const fbFrame = isFb && media.length > 0
    && (imgDead || fbLinkDead(media[0].thumb || media[0].url));
  const tallFrame = /\/reel\/|\/videos\//.test(String(t.url || ""));
  return (
    <article className={`card${media.length ? "" : " nomedia"}`
                        + (fbFrame ? " fbframe" : "")}>
      <div>
        <div className="chead">
          <Pfp t={t} name={name} />
          <b>{name}</b>
          <span className="handle">@{t.author_username}</span>
          {(() => {
            const P = { instagram: ["ig", "IG"], facebook: ["fb", "f"] }[t.platform] || ["x", "𝕏"];
            return <span className={`badge platform-${P[0]}`}>{P[1]}</span>;
          })()}
          {t.lag_ms != null && (
            <span className="badge lag">
              lag <b>{fmtLag(t.lag_ms)}</b>
            </span>
          )}
          {t.is_retweet ? <span className="badge rt">RT</span> : null}
          <LabelChip t={t} cats={cats} />
        </div>
        <p className="ctext">{withLinks(t.text, terms)}</p>
        <div className="cwl" title={t.created_at
          ? `posted ${new Date(t.created_at).toLocaleString("en-IN")}` : ""}>
          {t.streams?.length ? `Stream: ${t.streams.join(", ")} · ` : ""}
          collected {fmtAgo(t.collected_at)} · posted {fmtPosted(t.created_at)}
        </div>
        <div className="cstats">
          <span>❤ {fmtN(t.like_count ?? t.metrics?.likes)}</span>
          <span>↻ {fmtN(t.retweet_count)}</span>
          <span>💬 {fmtN(t.reply_count ?? t.metrics?.comments)}</span>
          <span>👁 {fmtN(t.view_count ?? t.metrics?.views)}</span>
          {t.lang ? <span>{String(t.lang).toUpperCase()}</span> : null}
        </div>
        <div className="cactions">
          <a href={t.url} target="_blank" rel="noreferrer">
            Open on {{ instagram: "Instagram", facebook: "Facebook" }[t.platform] || "X"}
          </a>
          <button onClick={() => navigator.clipboard?.writeText(t.url)}>Copy link</button>
          {/* Every platform can be pinned now: boards key on
              (platform, post_id), not on an X tweet id. */}
          {onPin && <button onClick={() => onPin(t)}>+ Collection</button>}
          {onUnpin && <button onClick={() => onUnpin(t)}>Unpin</button>}
          {onLabel && <LabelPicker t={t} cats={cats} onLabel={onLabel} />}
          {/* Not offered when the links have expired: a download that can
              only return "URL signature expired" is worse than no button. */}
          {media.length > 0 && !fbFrame && (
            <a href={media[0].url || media[0].thumb} target="_blank" rel="noreferrer" download>
              Download media
            </a>
          )}
        </div>
      </div>
      {fbFrame
        ? <FbEmbed url={t.url} tall={tallFrame} />
        : <Media media={media} onDead={isFb ? () => setImgDead(true) : null} />}
    </article>
  );
}
