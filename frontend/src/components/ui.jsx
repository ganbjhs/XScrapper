// Shared pieces: states, modal, toasts, icons.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export function Loading({ label = "Loading…" }) {
  return (
    <div className="state" role="status">
      <div className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

export function ErrorState({ error, retry }) {
  return (
    <div className="state error" role="alert">
      <b>Could not load</b>
      <div style={{ marginBottom: retry ? 12 : 0 }}>{String(error)}</div>
      {retry && (
        <button className="btn btn-ghost btn-sm" onClick={() => retry()}>
          Try again
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children }) {
  return (
    <div className="state">
      <b>{title}</b>
      {children}
    </div>
  );
}

// Rendered through a portal to <body>: the sticky navbar creates its own
// stacking context, so an overlay mounted inside it could never cover main.
export function Modal({ title, sub, onClose, children, wide = false }) {
  useEffect(() => {
    const h = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", h);
    return () => removeEventListener("keydown", h);
  }, [onClose]);
  return createPortal(
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}
           style={wide ? { width: "min(94vw, 620px)" } : undefined}>
        <h3>{title}</h3>
        {sub && <div className="sub">{sub}</div>}
        <AutoHeight>{children}</AutoHeight>
      </div>
    </div>,
    document.body,
  );
}

// Animates its own height whenever the content inside changes size, so a form
// that swaps fields glides instead of snapping.
export function AutoHeight({ children, className = "" }) {
  const inner = useRef(null);
  const [h, setH] = useState(null);
  useLayoutEffect(() => {
    const el = inner.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setH(el.offsetHeight));
    ro.observe(el);
    setH(el.offsetHeight);
    return () => ro.disconnect();
  }, []);
  return (
    <div className={`autoh ${className}`} style={h == null ? undefined : { height: h }}>
      <div ref={inner}>{children}</div>
    </div>
  );
}

// Segmented control: one visible choice per option, no dropdown.
export function Segmented({ value, options, onChange, className = "" }) {
  return (
    <div className={`seg ${className}`} role="radiogroup">
      {options.map(([v, label, tip]) => (
        <button key={v} type="button" role="radio" aria-checked={String(v) === String(value)}
                className={String(v) === String(value) ? "on" : ""} title={tip}
                onClick={() => onChange(v)}>
          {label}
        </button>
      ))}
    </div>
  );
}

/* ---------------- toasts ---------------- */
const listeners = new Set();
let seq = 0;

// toast("Saved") · toast.ok("Added") · toast.err("Failed: …") · toast.warn("…")
export function toast(message, { kind = "info", title = "", ms = 3800 } = {}) {
  const t = { id: ++seq, message: String(message), kind, title, ms };
  listeners.forEach((l) => l(t));
  return t.id;
}
toast.ok = (m, o) => toast(m, { ...o, kind: "ok" });
toast.err = (m, o) => toast(m, { ...o, kind: "err", ms: 6000 });
toast.warn = (m, o) => toast(m, { ...o, kind: "warn", ms: 5000 });

const GLYPH = { info: "i", ok: "✓", err: "!", warn: "!" };

export function ToastHost() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    const add = (t) => {
      setItems((xs) => [...xs.slice(-4), t]);
      setTimeout(() => dismiss(t.id), t.ms);
    };
    listeners.add(add);
    return () => listeners.delete(add);
  }, []);
  const dismiss = (id) => {
    setItems((xs) => xs.map((x) => (x.id === id ? { ...x, out: true } : x)));
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 200);
  };
  if (!items.length) return null;
  return createPortal(
    <div className="toasts" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}${t.out ? " out" : ""}`} role="status">
          <span className="ticon" aria-hidden="true">{GLYPH[t.kind]}</span>
          <div className="tmsg">
            {t.title && <b>{t.title}</b>}
            {t.message}
          </div>
          <button className="tx" onClick={() => dismiss(t.id)} aria-label="Dismiss">×</button>
        </div>
      ))}
    </div>,
    document.body,
  );
}

/* ---------------- keyboard shortcuts help ---------------- */
export const SHORTCUTS = [
  { h: "Navigate (press g, then a key)" },
  { k: ["g", "f"], d: "Live Feed" },
  { k: ["g", "w"], d: "Watchlists" },
  { k: ["g", "s"], d: "Search" },
  { k: ["g", "c"], d: "Collections" },
  { k: ["g", "a"], d: "Alerts" },
  { k: ["g", "d"], d: "Delivery" },
  { k: ["g", "l"], d: "Activity Log" },
  { k: ["g", "u"], d: "Accounts & Sessions" },
  { k: ["g", "g"], d: "Guard" },
  { k: ["g", "t"], d: "Settings" },
  { h: "General" },
  { k: ["/"], d: "Focus the first search / filter box" },
  { k: ["["], d: "Collapse or expand the sidebar" },
  { k: ["\\"], d: "Cycle theme (system → light → dark)" },
  { k: ["?"], d: "Show this help" },
  { k: ["Esc"], d: "Close dialogs" },
];

export function ShortcutsModal({ onClose }) {
  return (
    <Modal title="Keyboard shortcuts" sub="Shortcuts are ignored while you type in a field." onClose={onClose}>
      <div className="kbd-list">
        {SHORTCUTS.map((s, i) =>
          s.h ? (
            <div key={i} className="kh">{s.h}</div>
          ) : (
            <React.Fragment key={i}>
              <span>{s.d}</span>
              <span className="keys">{s.k.map((k) => <kbd key={k}>{k}</kbd>)}</span>
            </React.Fragment>
          ),
        )}
      </div>
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
      </div>
    </Modal>
  );
}

const I = (d, extra = null) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={d} />
    {extra}
  </svg>
);

export const icons = {
  feed: I("M2 12h4l3-8 4 16 3-8h6"),
  watchlists: I("M3.5 19c.7-3 2.9-4.5 5.5-4.5s4.8 1.5 5.5 4.5M16 4.5h5M16 8.5h5M16 12.5h3",
    <circle cx="9" cy="8" r="3.2" />),
  search: I("M21 21l-4.5-4.5", <circle cx="11" cy="11" r="7" />),
  collections: I("M6 3h12v18l-6-4-6 4z"),
  alerts: I("M18 8a6 6 0 10-12 0c0 7-3 8-3 8h18s-3-1-3-8M10 21a2 2 0 004 0"),
  delivery: I("M22 2L11 13M22 2l-7 20-4-9-9-4z"),
  activity: I("M12 8v4l2.5 2.5", <circle cx="12" cy="12" r="9" />),
  accounts: I("M5 21c.9-3.7 3.6-5.5 7-5.5s6.1 1.8 7 5.5", <circle cx="12" cy="7" r="3.5" />),
  guard: I("M12 3l8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7z"),
  stress: I("M13 2L4 14h7l-1 8 9-12h-7z"),
  settings: I("M12 15.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7M19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.6 1.7 1.7 0 00-1.9.4l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.6-1.1 1.7 1.7 0 00-.4-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.9.3H11a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.9V11a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"),
  menu: I("M4 6h16M4 12h16M4 18h16"),
  chevron: I("M6 9l6 6 6-6"),
  collapse: I("M15 6l-6 6 6 6"),
  sun: I("M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4", <circle cx="12" cy="12" r="4" />),
  moon: I("M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"),
  auto: I("M12 3a9 9 0 100 18 9 9 0 000-18zm0 0v18", <path d="M12 3a9 9 0 010 18z" fill="currentColor" stroke="none" />),
  keyboard: I("M6 10h.01M10 10h.01M14 10h.01M18 10h.01M8 14h8", <rect x="2" y="6" width="20" height="12" rx="2" />),
  logout: I("M15 17l5-5-5-5M20 12H9M13 21H5a2 2 0 01-2-2V5a2 2 0 012-2h8"),
  edit: I("M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"),
  back: I("M15 18l-6-6 6-6"),
  info: I("M12 16v-4M12 8h.01", <circle cx="12" cy="12" r="9" />),
  more: I("M4 6h16M4 12h16M4 18h16"),
  x: I("M18 6L6 18M6 6l12 12"),
  tower: I("M9 21l1.5-9h3L15 21M8 3h8M9 3l1 5h4l1-5M8 12h8"),
};

// Small ⓘ that explains on hover or focus — the paragraph lives in a tooltip
// rendered through a portal, so no scrolling box can clip it.
export function Hint({ text }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const below = r.bottom + 8 + 120 < innerHeight;
    setPos({ x: Math.min(Math.max(164, r.left + r.width / 2), innerWidth - 164), y: below ? r.bottom + 8 : r.top - 8, below });
  };
  const hide = () => setPos(null);
  return (
    <>
      <span ref={ref} className="hint-i" tabIndex={0} role="img" aria-label={text}
            onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
        {icons.info}
      </span>
      {pos && createPortal(
        <div className={`tipbox ${pos.below ? "below" : "above"}`} role="tooltip"
             style={{ left: pos.x, top: pos.y }}>{text}</div>,
        document.body,
      )}
    </>
  );
}

// A labelled block inside a detail panel: label + optional hint on the left,
// optional controls on the right, content below. Keeps every panel the same shape.
export function Sec({ label, hint, right, children, className = "" }) {
  return (
    <section className={`sec ${className}`}>
      <div className="sec-h">
        <span className="sec-l">{label}{hint && <Hint text={hint} />}</span>
        {right && <span className="sec-r">{right}</span>}
      </div>
      {children}
    </section>
  );
}

// A dropdown that looks like a solid pill: label + current value + caret,
// with the native <select> stretched invisibly over it (see .fpill-block).
export function PillSelect({ label, value, options, onChange, disabled, title, className = "" }) {
  const cur = options.find(([v]) => String(v) === String(value ?? ""));
  return (
    <label className={`fpill fpill-block ${className}`} title={title}>
      <span>{label}</span>
      <span className="fpill-val">{cur ? cur[1] : String(value ?? "")}</span>
      <svg className="fpill-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
      <select value={value ?? ""} disabled={disabled} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
      </select>
    </label>
  );
}

/* ---------------- Watch-Tower consumer badge ----------------
   A tower icon with one letter per platform: lit while Watch-Tower pulled that
   platform within the live window, grey when it has gone quiet. Hover lists
   the project and each platform's last pull. */
const WT_PLATFORMS = [["x", "𝕏", "X"], ["instagram", "IG", "Instagram"], ["facebook", "f", "Facebook"], ["links", "↗", "Post links"]];

function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function wtStatus(consumers, projectId) {
  const row = consumers?.projects?.[String(projectId)];
  if (!row) return null;
  return row;
}

export function WtBadge({ consumers, project, compact = false }) {
  const row = wtStatus(consumers, project?.project_id);
  const ref = useRef(null);
  const [pos, setPos] = useState(null);
  // Listed-only projects (Watch-Tower shows them but is not bound) get no badge.
  if (!row || !row.mirrored) return null;
  const plats = WT_PLATFORMS.filter(([k]) => row.platforms?.[k]);
  const live = row.live;
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (r) setPos({ x: Math.min(Math.max(150, r.left + r.width / 2), innerWidth - 150), y: r.bottom + 8 });
  };
  return (
    <>
      <span ref={ref} className={`wt ${live ? "live" : "idle"}${compact ? " compact" : ""}`}
            onMouseEnter={show} onMouseLeave={() => setPos(null)} onFocus={show} onBlur={() => setPos(null)}
            tabIndex={0} aria-label={`Watch-Tower ${live ? "live" : "idle"}`}>
        {icons.tower}
        {!compact && plats.map(([k, glyph]) => (
          <b key={k} className={row.platforms[k].live ? "on" : ""}>{glyph}</b>
        ))}
      </span>
      {pos && createPortal(
        <div className="tipbox below wt-tip" style={{ left: pos.x, top: pos.y }}>
          <b>{project.name}</b> — {live ? "bound · Watch-Tower is mirroring it now" : `bound · last mirror pull ${ago(row.last_ms)}`}
          <div className="wt-rows">
            {plats.map(([k, glyph, name]) => (
              <span key={k} className={row.platforms[k].live ? "on" : ""}>
                <i className="dot" /> {name} · {ago(row.platforms[k].last_ms)}
              </span>
            ))}

          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

export function useMediaQuery(q) {
  const [m, setM] = useState(() => typeof matchMedia !== "undefined" && matchMedia(q).matches);
  useEffect(() => {
    const mq = matchMedia(q);
    const h = () => setM(mq.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, [q]);
  return m;
}
