// Small shared pieces: states, modal, icons. Everything renders honest
// loading / empty / error rather than a blank area.
import React, { useEffect } from "react";
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

// An overlay is rendered THROUGH A PORTAL to <body>, never in place.
//
// z-index only ranks siblings inside the same stacking context, and the modal
// is usually mounted deep inside whatever component opened it. `nav.side` is
// `position: sticky`, and a sticky element ALWAYS creates a stacking context
// even with `z-index: auto` — so the "New project" modal, which lives inside
// the project switcher in the navbar, had its `z-index: 50` scoped to the
// inside of the navbar. `main.content` comes after the navbar in the DOM, so
// every positioned descendant of the feed painted over it: the post media
// thumbnails (`.thumb` is `position: relative`) sat on top of the dialog and
// its scrim, un-dimmed, covering the name field (2026-08-25).
//
// Raising the z-index would not have fixed it — no value inside a trapped
// context can beat a sibling of the context itself. The portal is the fix:
// mounted on <body>, the overlay has no ancestor to be trapped by, and it
// keeps working wherever a future caller happens to mount it. React events
// still bubble through the portal to the React parent, so callers are
// unchanged.
export function Modal({ title, sub, onClose, children }) {
  useEffect(() => {
    const h = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", h);
    return () => removeEventListener("keydown", h);
  }, [onClose]);
  return createPortal(
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        {sub && <div className="sub">{sub}</div>}
        {children}
      </div>
    </div>,
    document.body,
  );
}

const I = (d, extra = null) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
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
  menu: I("M4 6h16M4 12h16M4 18h16"),
};
