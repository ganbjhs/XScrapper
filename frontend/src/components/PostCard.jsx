// One collected post — X or Instagram — with the Collector's own chrome:
// platform badge, lag badge, watchlist attribution, media thumbnails.
import React from "react";
import { fmtAgo, fmtLag, fmtN, fmtPosted } from "../api/client.js";

const PFP_COLORS = ["#2a6b46", "#a8552e", "#7a4a9e", "#3f6b8f", "#8a6100", "#54303f"];
const pfpColor = (s) => {
  let h = 0;
  for (const c of String(s || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PFP_COLORS[h % PFP_COLORS.length];
};

function Media({ media }) {
  if (!media?.length) return null;
  const shown = media.slice(0, 2);
  const extra = media.length - shown.length;
  return (
    <div className="media-col">
      {shown.map((m, i) => (
        <a key={i} className="thumb" href={m.url || m.thumb} target="_blank" rel="noreferrer">
          {(m.thumb || m.url) && (
            <img src={m.thumb || m.url} alt={m.type} loading="lazy"
                 onError={(e) => { e.currentTarget.style.display = "none"; }} />
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
  const esc = terms
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

export default function PostCard({ t, onPin, onUnpin, terms }) {
  const name = t.author_display_name || t.author_username || "unknown";
  const media = t.media || [];
  return (
    <article className={`card${media.length ? "" : " nomedia"}`}>
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
          {onPin && t.platform === "x" && (
            <button onClick={() => onPin(t)}>+ Collection</button>
          )}
          {onUnpin && <button onClick={() => onUnpin(t)}>Unpin</button>}
          {media.length > 0 && (
            <a href={media[0].url || media[0].thumb} target="_blank" rel="noreferrer" download>
              Download media
            </a>
          )}
        </div>
      </div>
      <Media media={media} />
    </article>
  );
}
