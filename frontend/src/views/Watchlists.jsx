// Watchlists — ONE structure for every platform.
//
// Layout contract (the fix for "everything thrown on the main interface"):
//   * Two tabs: "Watchlists" (the daily surface) and "Network & settings"
//     (configuration, login health, streams wiring — the rarely-used things).
//   * The Watchlists tab is MASTER-DETAIL: a compact list of every watchlist
//     across X / Facebook / Instagram on the left, ONE detail panel on the
//     right. A list with 200 handles scrolls inside its own box, never the
//     page.
//   * "+ New watchlist" is a single flow for every platform: pick the
//     platform first, the form adapts (X: handles / keywords / X List;
//     Facebook: pages / favorites; Instagram: user / hashtag / following).
//     A future platform adds one entry to PLATFORM_KINDS and one detail
//     component — the shell does not change.
import React, { useMemo, useState } from "react";
import { api, fmtAgo, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

// Must match FB_SPEEDS in web.py — the named cadences a page can be checked at.
const FB_SPEEDS = { "1h": "1 hour", "3h": "3 hours", "6h": "6 hours",
                    "12h": "12 hours", "24h": "24 hours" };

const INTERVAL_OPTS = [
  ["900", "15 minutes"], ["1800", "30 minutes"], ["3600", "1 hour"],
  ["10800", "3 hours"], ["21600", "6 hours"], ["43200", "12 hours"],
  ["86400", "24 hours"],
];

const splitAdd = (kind, raw) =>
  kind === "keywords"
    ? raw.split(/,|\n/).map((s) => s.trim()).filter(Boolean)
    : raw.split(/[\s,]+/).filter(Boolean);

// What each platform's watchlist can be — drives the Add modal.
const PLATFORM_KINDS = {
  x: [
    ["query", "Handles (built here — no X List needed)"],
    ["keywords", "Keywords (topics, phrases, AND combinations)"],
    ["xlist", "Existing X List (fastest polling)"],
  ],
  fb: [
    ["pages", "Pages (each page checked on its own cadence)"],
    ["favorites", "Favorites feed (one richer pass over the account's Favorites)"],
  ],
  ig: [
    ["user", "User (a profile's posts — numeric id preferred)"],
    ["hashtag", "Hashtag"],
    ["following", "Home feed (everything the account follows)"],
  ],
};

// ---------------------------------------------------------------------------
// The unified Add modal — platform first, then the platform's own form.
// ---------------------------------------------------------------------------

function AddModal({ pid, onDone, onClose }) {
  const [platform, setPlatform] = useState("x");
  const [kind, setKind] = useState("query");
  const [name, setName] = useState("");
  const [listId, setListId] = useState("");
  const [handles, setHandles] = useState("");
  const [igValue, setIgValue] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const pick = (p) => { setPlatform(p); setKind(PLATFORM_KINDS[p][0][0]); setErr(""); };

  const create = async () => {
    setBusy(true); setErr("");
    try {
      if (platform === "x") {
        const body = { project: pid, name, kind };
        if (kind === "xlist") body.list_id = listId;
        else if (kind === "keywords")
          body.handles = handles.split(/\n+/).map((s) => s.trim()).filter(Boolean);
        else body.handles = handles.split(/[\s,]+/).filter(Boolean);
        const made = await api.createWatchlist(body);
        if (made.warning) { setErr(made.warning); return; }
      } else if (platform === "fb") {
        const names = handles.split(/[\s,]+/).filter(Boolean);
        if (kind === "favorites") await api.fbSettings({ mode: "favorites" });
        for (const n of names) await api.fbAddSource(pid, n);
      } else if (platform === "ig") {
        await api.igSource({ action: "add", label: name.trim(),
                             type: kind, value: igValue.trim() });
      }
      onDone(platform); onClose();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const canCreate =
    platform === "x" ? name.trim() && (kind === "xlist" ? listId.trim() : true)
      : platform === "fb" ? (kind === "favorites" || handles.trim())
      : name.trim() && (kind === "following" || igValue.trim());

  return (
    <Modal title="New watchlist" onClose={onClose}
           sub="One flow for every platform — pick where it collects from, the form adapts.">
      <div className="field">
        <label>Platform</label>
        <select value={platform} onChange={(e) => pick(e.target.value)}>
          <option value="x">X (Twitter)</option>
          <option value="fb">Facebook</option>
          <option value="ig">Instagram</option>
        </select>
      </div>
      <div className="field">
        <label>Type</label>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {PLATFORM_KINDS[platform].map(([v, t]) => (
            <option key={v} value={v}>{t}</option>
          ))}
        </select>
      </div>

      {platform === "x" && (
        <>
          <div className="field">
            <label>Name</label>
            <input value={name} autoFocus onChange={(e) => setName(e.target.value)}
                   placeholder="e.g. Cabinet" />
          </div>
          {kind === "xlist" ? (
            <div className="field">
              <label>X List URL or id</label>
              <input value={listId} onChange={(e) => setListId(e.target.value)}
                     placeholder="https://x.com/i/lists/1234567890123456789" />
            </div>
          ) : kind === "keywords" ? (
            <div className="field">
              <label>Keywords — one rule per line</label>
              <textarea rows="5" value={handles} onChange={(e) => setHandles(e.target.value)}
                        placeholder={'finance AND gst\n"vishnu deo sai"\n#Chhattisgarh'} />
              <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
                Lines combine as OR; <b>AND</b> means both words must appear.
                Quotes = exact phrase. X search operators pass through.
              </div>
            </div>
          ) : (
            <div className="field">
              <label>Handles — one per line, @ optional</label>
              <textarea rows="5" value={handles} onChange={(e) => setHandles(e.target.value)}
                        placeholder={"@DrKirodilalBJP\nJoraramKumawat\nhttps://x.com/KirodiOffice"} />
            </div>
          )}
        </>
      )}

      {platform === "fb" && (
        <>
          {kind === "favorites" && (
            <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "10px 0 0", lineHeight: 1.5 }}>
              Switches collection to the account's <b>Favorites feed</b> — one
              richer pass instead of page-by-page checks. Add the pages below
              too so posts are attributed to them (and add them to Favorites
              in the collector's Facebook account: Feeds → Favourites → Manage).
            </div>
          )}
          <div className="field">
            <label>Page handles — from the page URL, one per line</label>
            <textarea rows="4" value={handles} onChange={(e) => setHandles(e.target.value)}
                      placeholder={"narendramodi\nAmitShahOfficial"} />
          </div>
        </>
      )}

      {platform === "ig" && (
        <>
          <div className="field">
            <label>Label (shown in the list)</label>
            <input value={name} autoFocus onChange={(e) => setName(e.target.value)}
                   placeholder="e.g. natgeo" />
          </div>
          {kind !== "following" && (
            <div className="field">
              <label>{kind === "user" ? "User — numeric id preferred (or username)" : "Hashtag (without #)"}</label>
              <input value={igValue} onChange={(e) => setIgValue(e.target.value)}
                     placeholder={kind === "user" ? "787132" : "wildlife"} />
              {kind === "user" && (
                <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
                  The numeric id keeps working even when the session is
                  restricted — find it in the profile page source as “profile_id”.
                </div>
              )}
            </div>
          )}
        </>
      )}

      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !canCreate} onClick={create}>
          Create
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// X detail panel
// ---------------------------------------------------------------------------

const FILTER_BOXES = [
  ["skip_retweets", "No retweets"],
  ["skip_quotes", "No quote tweets"],
  ["skip_replies", "No replies"],
  ["only_media", "Only posts with media"],
  ["skip_links", "No link posts"],
  ["verified_only", "Verified (blue) only"],
];

function FiltersPanel({ w, onChanged }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState(w.filters || {});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const active = Object.keys(w.filters || {}).length;
  const save = async () => {
    setBusy(true); setMsg("");
    try {
      await api.watchlistFilters(w.watchlist_id, f);
      setMsg("✓ Saved — collection uses the new filters from its next check");
      onChanged();
    } catch (e) { setMsg(`✗ ${String(e.message || e)}`); }
    finally { setBusy(false); }
  };
  const box = (key, label) => (
    <label className="check" key={key}>
      <input type="checkbox" checked={!!f[key]}
             onChange={(e) => setF((s) => ({ ...s, [key]: e.target.checked }))} />
      {label}
    </label>
  );

  return (
    <div style={{ marginTop: 12, borderTop: "1px solid var(--ring)", paddingTop: 10 }}>
      <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>
        Collection filters{active ? ` (${active} active)` : ""} {open ? "▴" : "▾"}
      </button>
      {open && (
        <div style={{ marginTop: 10 }}>
          <div className="filters" style={{ marginBottom: 8 }}>
            {FILTER_BOXES.map(([k, l]) => box(k, l))}
          </div>
          <div className="filters" style={{ marginBottom: 8 }}>
            <input placeholder="language (hi, en…)" value={f.lang || ""}
                   style={{ width: 150 }}
                   onChange={(e) => setF((s) => ({ ...s, lang: e.target.value }))} />
            <input placeholder="min likes" inputMode="numeric" value={f.min_likes || ""}
                   style={{ width: 110 }}
                   onChange={(e) => setF((s) => ({ ...s, min_likes: e.target.value }))} />
            <input placeholder="min retweets" inputMode="numeric" value={f.min_retweets || ""}
                   style={{ width: 120 }}
                   onChange={(e) => setF((s) => ({ ...s, min_retweets: e.target.value }))} />
            <button className="btn btn-brand btn-sm" disabled={busy} onClick={save}>
              Save filters
            </button>
          </div>
          {msg && (
            <div className={msg.startsWith("✓") ? "st-good" : "st-crit"}
                 style={{ fontSize: 12.5, fontWeight: 600 }}>{msg}</div>
          )}
          <div style={{ color: "var(--ink-3)", fontSize: 12 }}>
            Applies at collection time — filtered posts are never fetched at
            all. Already-collected posts stay.
          </div>
        </div>
      )}
    </div>
  );
}

function XDetail({ w, onChanged }) {
  const [adding, setAdding] = useState("");
  const [search, setSearch] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(null);   // {old, val}

  const change = async (add, remove) => {
    setBusy(true); setErr("");
    try {
      await api.watchlistMembers(w.watchlist_id, add, remove);
      setAdding(""); onChanged();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const live = w.streams.filter((s) => !s.paused);
  const collected = w.streams.reduce((a, s) => a + (s.tweets || 0), 0);
  const setInterval = async (seconds) => {
    await api.watchlistInterval(w.watchlist_id, seconds);
    onChanged();
  };
  const curInterval = w.interval_s ? String(w.interval_s) : "";
  const members = search
    ? w.members.filter((m) => m.handle.toLowerCase().includes(search.toLowerCase()))
    : w.members;

  return (
    <div className="panel">
      <div className="phead" style={{ alignItems: "center", flexWrap: "wrap", rowGap: 8 }}>
        <h3>{w.name}</h3>
        <span className="badge platform-x">
          {w.kind === "xlist" ? "X List" : w.kind === "keywords" ? "keywords" : "handles"}
        </span>
        <span className="chip">{fmtN(collected)} collected</span>
        <span className="right" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label className="fpill" style={{ padding: "5px 6px 5px 11px" }}
                 title="how often the collector re-checks this watchlist">
            <span>every</span>
            <select value={curInterval} onChange={(e) => setInterval(e.target.value)}>
              <option value="">auto</option>
              <option value="300">5 min</option>
              <option value="600">10 min</option>
              <option value="900">15 min</option>
              <option value="1800">30 min</option>
              <option value="3600">1 hour</option>
            </select>
          </label>
          <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}>
            Delete
          </button>
        </span>
      </div>
      <div style={{ color: "var(--ink-3)", fontSize: 12.5 }}>
        {w.kind === "xlist"
          ? `Collected through X List ${w.list_id} — members are managed on x.com.`
          : `${w.members.length} ${w.kind === "keywords" ? "keyword rule" : "handle"}${w.members.length === 1 ? "" : "s"} → ${live.length} live stream${live.length === 1 ? "" : "s"}`}
      </div>

      {w.kind !== "xlist" && (
        <>
          {w.members.length > 12 && (
            <div className="filters" style={{ margin: "10px 0 0" }}>
              <input value={search} placeholder={`search ${w.members.length} members…`}
                     style={{ flex: 1, minWidth: 160 }}
                     onChange={(e) => setSearch(e.target.value)} />
            </div>
          )}
          <div className="members-box">
            {members.map((mb) => (
              <span className="tag" key={mb.handle}>
                <button style={{ font: "inherit", color: "inherit", padding: 0 }}
                        title="click to edit" disabled={busy}
                        onClick={() => setEditing({ old: mb.handle, val: mb.handle })}>
                  {w.kind === "keywords" ? mb.handle : `@${mb.handle}`}
                </button>
                <button aria-label={`remove ${mb.handle}`} disabled={busy}
                        onClick={() => change([], [mb.handle])}>✕</button>
              </span>
            ))}
            {w.members.length === 0 && (
              <span style={{ color: "var(--ink-3)", fontSize: 13 }}>
                {w.kind === "keywords" ? "No keywords yet — add some below."
                                       : "No handles yet — add some below."}
              </span>
            )}
            {w.members.length > 0 && members.length === 0 && (
              <span style={{ color: "var(--ink-3)", fontSize: 13 }}>no match for “{search}”</span>
            )}
          </div>
          <div className="filters" style={{ marginBottom: 0 }}>
            <input value={adding}
                   placeholder={w.kind === "keywords"
                     ? "finance AND gst — or several rules separated by commas"
                     : "@handle, profile URL, or several separated by spaces"}
                   style={{ flex: 1, minWidth: 200 }}
                   onChange={(e) => setAdding(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && adding.trim() &&
                     change(splitAdd(w.kind, adding), [])} />
            <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()}
                    onClick={() => change(splitAdd(w.kind, adding), [])}>
              Add
            </button>
          </div>
        </>
      )}
      {err && <div style={{ color: "var(--critical)", fontSize: 13, marginTop: 8 }}>{err}</div>}

      {w.kind !== "xlist" && <FiltersPanel w={w} onChanged={onChanged} />}

      {editing && (
        <Modal title={w.kind === "keywords" ? "Edit keyword rule" : "Edit handle"}
               sub="The collection query rebuilds automatically on save."
               onClose={() => setEditing(null)}>
          <div className="field">
            <label htmlFor="edm">{w.kind === "keywords" ? "Rule" : "Handle"}</label>
            <input id="edm" value={editing.val} autoFocus
                   onChange={(e) => setEditing((s) => ({ ...s, val: e.target.value }))}
                   onKeyDown={(e) => e.key === "Enter" && editing.val.trim() &&
                     (change([editing.val], [editing.old]), setEditing(null))} />
          </div>
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-brand" disabled={!editing.val.trim() || busy}
                    onClick={() => { change([editing.val], [editing.old]); setEditing(null); }}>
              Save
            </button>
          </div>
        </Modal>
      )}

      {confirming && (
        <Modal title={`Delete “${w.name}”?`} onClose={() => setConfirming(false)}
               sub="Collection stops. Everything already collected stays in the database.">
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setConfirming(false)}>Keep it</button>
            <button className="btn btn-danger"
                    onClick={async () => {
                      await api.removeWatchlist(w.watchlist_id);
                      setConfirming(false); onChanged();
                    }}>
              Delete
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Facebook detail panel — pages + fetch. Configuration lives in the
// Network & settings tab, NOT here.
// ---------------------------------------------------------------------------

function FbDetail({ pid, data, reload, gotoSettings }) {
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [fetching, setFetching] = useState(false);
  const [result, setResult] = useState(null);
  const sources = data?.sources || [];
  const paused = !!data?.paused;
  const health = data?.health || {};

  const add = async () => {
    setBusy(true); setMsg("");
    try {
      for (const name of adding.split(/[\s,]+/).filter(Boolean)) {
        await api.fbAddSource(pid, name);
      }
      setAdding(""); reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const run = (fn, favorites) => async () => {
    setFetching(true); setResult(null); setMsg("");
    try {
      const r = await fn(pid);
      if (r.error) setMsg(r.error);
      else setResult(favorites ? { ...r, favorites: true } : r);
      reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setFetching(false); }
  };

  return (
    <div className="panel">
      <div className="phead" style={{ alignItems: "center", flexWrap: "wrap", rowGap: 8 }}>
        <h3><span className="badge platform-fb" style={{ marginRight: 8 }}>f</span>Facebook pages</h3>
        <span className="chip">{fmtN(data?.totals?.posts ?? 0)} collected</span>
        <span className={`chip ${paused ? "warn" : "good"}`}>{paused ? "paused" : "collecting"}</span>
        {data?.config?.mode === "favorites" && <span className="chip">favorites mode</span>}
        {(health.blocked || !data?.enabled) && (
          <button className="chip crit" style={{ cursor: "pointer" }} onClick={gotoSettings}
                  title="Open Network & settings to fix the login">
            {health.blocked ? "login needs a human →" : "login not set up →"}
          </button>
        )}
        <span className="right">{sources.length} page{sources.length === 1 ? "" : "s"}</span>
      </div>

      <div className="toolbar">
        <button className="btn btn-brand btn-sm"
                disabled={fetching || paused || sources.length === 0}
                onClick={run(api.fbFetch)}>
          {fetching ? "Fetching…" : "Fetch now"}
        </button>
        <button className="btn btn-ghost btn-sm"
                disabled={fetching || paused || sources.length === 0}
                onClick={run(api.fbFavorites, true)}
                title="Read the account's Favorites feed once — richer data, one pass">
          {fetching ? "…" : "Fetch Favorites feed"}
        </button>
        <span className="grow" />
        <button className="btn btn-ghost btn-sm" onClick={gotoSettings}>
          Settings →
        </button>
      </div>

      {fetching && (
        <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "6px 0" }}>
          Opening Facebook on the server and reading newest posts — up to a minute.
        </div>
      )}
      {result && (
        <div style={{ fontSize: 12.5, margin: "6px 0" }}>
          <b style={{ color: result.new > 0 ? "var(--brand)" : "var(--ink-2)" }}>
            {result.new > 0
              ? `${result.new} new post${result.new === 1 ? "" : "s"} collected`
              : "No new posts this time"}
          </b>{" "}
          — open the Live Feed (Source: Facebook) to see them.
          {Array.isArray(result.log) && result.log.length > 0 && (
            <pre style={{ whiteSpace: "pre-wrap", background: "var(--brand-softer)",
                          padding: "8px 10px", borderRadius: 8, marginTop: 6,
                          fontSize: 11.5, color: "var(--ink-3)", maxHeight: 160,
                          overflow: "auto" }}>
              {result.log.join("\n")}
            </pre>
          )}
        </div>
      )}

      <div className="members-box" style={{ maxHeight: 320, padding: "0 12px" }}>
        {sources.map((s) => (
          <div className="wl-row" key={s.label} style={{ opacity: s.enabled ? 1 : 0.55 }}>
            <div className="who">
              <b>{s.label}{!s.enabled && " (paused)"}</b>
              <small>
                {fmtN(s.posts)} collected ·{" "}
                {s.last_run ? `checked ${fmtAgo(s.last_run * 1000)}` : "not checked yet"}
              </small>
            </div>
            <div className="right" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <label className="fpill" title="how often this page is checked"
                     style={{ padding: "4px 5px 4px 10px" }}>
                <span>every</span>
                <select value={s.speed || ""}
                        onChange={async (e) => { await api.fbSetInterval(s.label, e.target.value); reload(); }}>
                  <option value="">6h</option>
                  {Object.entries(FB_SPEEDS).map(([v, t]) => (
                    <option key={v} value={v}>{t}</option>
                  ))}
                </select>
              </label>
              <button className="btn btn-ghost btn-sm"
                      onClick={async () => { await api.fbSetEnabled(s.label, !s.enabled); reload(); }}>
                {s.enabled ? "Pause" : "Resume"}
              </button>
              <button className="btn btn-ghost btn-sm" aria-label={`remove ${s.label}`}
                      onClick={async () => { await api.fbRemoveSource(s.label); reload(); }}>
                Remove
              </button>
            </div>
          </div>
        ))}
        {sources.length === 0 && (
          <div style={{ color: "var(--ink-3)", fontSize: 13, padding: "12px 0" }}>
            No Facebook pages yet — add a page's handle (from its URL, e.g. “narendramodi”).
          </div>
        )}
      </div>

      <div className="filters" style={{ marginBottom: 0 }}>
        <input value={adding} placeholder="facebook page handle, e.g. narendramodi"
               style={{ flex: 1, minWidth: 200 }}
               onChange={(e) => setAdding(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && adding.trim() && add()} />
        <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()} onClick={add}>
          Add page
        </button>
      </div>
      {msg && <div style={{ color: "var(--critical)", fontSize: 12.5, marginTop: 8 }}>{msg}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Instagram detail panel
// ---------------------------------------------------------------------------

function IgDetail({ pid, data, reload, gotoSettings }) {
  const [msg, setMsg] = useState("");
  const [fetching, setFetching] = useState(false);
  const [result, setResult] = useState(null);
  const sources = data?.sources || [];
  const paused = !!data?.paused;
  const anyCheckpoint = (data?.accounts || []).some((a) => a.checkpoint_at);
  const anyActive = (data?.accounts || []).some((a) => a.active);
  const act = async (body) => {
    setMsg("");
    try { await api.igSource(body); reload(); }
    catch (e) { setMsg(String(e.message || e)); }
  };
  const fetchNow = async () => {
    setFetching(true); setResult(null); setMsg("");
    try {
      const r = await api.igFetch(pid);
      if (r.error) setMsg(r.error);
      else setResult(r);
      reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setFetching(false); }
  };

  return (
    <div className="panel">
      <div className="phead" style={{ alignItems: "center", flexWrap: "wrap", rowGap: 8 }}>
        <h3><span className="badge platform-ig" style={{ marginRight: 8 }}>IG</span>Instagram sources</h3>
        <span className="chip">{fmtN(data?.totals?.posts ?? 0)} collected</span>
        <span className={`chip ${paused ? "warn" : anyActive ? "good" : "warn"}`}>
          {paused ? "paused" : anyActive ? "collecting" : "no active session"}
        </span>
        {(anyCheckpoint || !anyActive) && (
          <button className="chip crit" style={{ cursor: "pointer" }} onClick={gotoSettings}
                  title="Open Network & settings to fix the session">
            {anyCheckpoint ? "checkpoint — needs a human →" : "not signed in →"}
          </button>
        )}
        <span className="right">{sources.length} source{sources.length === 1 ? "" : "s"}</span>
      </div>

      <div className="toolbar">
        <button className="btn btn-brand btn-sm"
                disabled={fetching || paused || sources.length === 0}
                onClick={fetchNow}>
          {fetching ? "Fetching…" : "Fetch now"}
        </button>
        <span className="grow" />
        <button className="btn btn-ghost btn-sm" onClick={gotoSettings}>Settings →</button>
      </div>

      {fetching && (
        <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "6px 0" }}>
          Polling Instagram through the account's session — a few seconds.
        </div>
      )}
      {result && (
        <div style={{ fontSize: 12.5, margin: "6px 0" }}>
          <b style={{ color: result.new > 0 ? "var(--brand)" : "var(--ink-2)" }}>
            {result.new > 0
              ? `${result.new} new post${result.new === 1 ? "" : "s"} collected`
              : "No new posts this time"}
          </b>{" "}— open the Live Feed to see them.
          {Array.isArray(result.log) && result.log.length > 0 && (
            <pre style={{ whiteSpace: "pre-wrap", background: "var(--brand-softer)",
                          padding: "8px 10px", borderRadius: 8, marginTop: 6,
                          fontSize: 11.5, color: "var(--ink-3)", maxHeight: 160,
                          overflow: "auto" }}>
              {result.log.join("\n")}
            </pre>
          )}
        </div>
      )}

      <div style={{ color: "var(--ink-3)", fontSize: 12.5 }}>
        Collected by the Instagram service on its own cadence — “Fetch now”
        runs one pass immediately. Use “+ New watchlist” above to add a user,
        hashtag, or the home feed.
      </div>
      <div className="members-box" style={{ maxHeight: 320, padding: "0 12px" }}>
        {sources.map((s) => (
          <div className="wl-row" key={s.label} style={{ opacity: s.enabled ? 1 : 0.55 }}>
            <div className="who">
              <b>{s.label}{!s.enabled && " (paused)"}</b>
              <small>{s.type}{s.value ? ` · ${s.value}` : ""}{s.account ? ` · @${s.account}` : ""}</small>
            </div>
            <div className="right" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <button className="btn btn-ghost btn-sm"
                      onClick={() => act({ action: "enable", label: s.label, enabled: !s.enabled })}>
                {s.enabled ? "Pause" : "Resume"}
              </button>
              <button className="btn btn-ghost btn-sm"
                      onClick={() => act({ action: "remove", label: s.label })}>
                Remove
              </button>
            </div>
          </div>
        ))}
        {sources.length === 0 && (
          <div style={{ color: "var(--ink-3)", fontSize: 13, padding: "12px 0" }}>
            No Instagram sources yet — use “+ New watchlist” and pick Instagram.
          </div>
        )}
      </div>
      {msg && <div style={{ color: "var(--critical)", fontSize: 12.5 }}>{msg}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Network & settings tab — configuration, login health, streams wiring.
// ---------------------------------------------------------------------------

function FbHealthBanner({ health, onAction, busy }) {
  if (!health?.blocked) return null;
  return (
    <div style={{ border: "1px solid var(--critical)", borderRadius: 10,
                  padding: "10px 12px", margin: "8px 0",
                  background: "color-mix(in srgb, var(--critical) 8%, transparent)" }}>
      <b className="st-crit">
        Login needs a human — automatic retries are stopped
        ({health.reason === "checkpoint" ? "verification checkpoint" : "login failed"})
      </b>
      <div style={{ fontSize: 12.5, marginTop: 4 }}>{health.detail}</div>
      <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4 }}>
        since {health.ts ? fmtAgo(health.ts * 1000) : "—"}
        {health.email ? ` · account ${health.email}` : ""}
      </div>
      <div className="filters" style={{ marginTop: 8, marginBottom: 0 }}>
        <button className="btn btn-brand btn-sm" disabled={busy}
                onClick={() => onAction("clear")}>
          I fixed it — clear &amp; retry
        </button>
        <button className="btn btn-ghost btn-sm" disabled={busy}
                onClick={() => onAction("reset_session")}
                title="Also deletes fb_state.json so the next run logs in completely fresh">
          Reset session (fresh login next run)
        </button>
      </div>
    </div>
  );
}

function FbSettings({ data, reload }) {
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const cfg = data?.config || {};
  const ses = data?.session || {};
  const paused = !!data?.paused;

  const f = form || {
    mode: cfg.mode || "pages",
    default_interval_s: String(cfg.default_interval_s || 21600),
    fav_interval_s: String(cfg.fav_interval_s || 3600),
  };

  const healthAction = async (action) => {
    setBusy(true);
    try { await api.fbHealthAction(action); reload(); }
    catch (e) { setNote(`✗ ${String(e.message || e)}`); }
    finally { setBusy(false); }
  };
  const togglePause = async () => {
    setBusy(true);
    try { await api.fbControl(paused ? "resume" : "pause"); reload(); }
    catch (e) { setNote(`✗ ${String(e.message || e)}`); }
    finally { setBusy(false); }
  };
  const save = async () => {
    setBusy(true); setNote("");
    try {
      await api.fbSettings(f);
      setNote("✓ Saved — the collector uses this from its next cycle (no restart)");
      reload();
    } catch (e) { setNote(`✗ ${String(e.message || e)}`); }
    finally { setBusy(false); }
  };

  return (
    <div className="panel">
      <div className="phead" style={{ alignItems: "center", flexWrap: "wrap", rowGap: 8 }}>
        <h3><span className="badge platform-fb" style={{ marginRight: 8 }}>f</span>Facebook network</h3>
        <span className={`chip ${paused ? "warn" : "good"}`}>{paused ? "paused" : "collecting"}</span>
        <span className={`chip ${!data?.enabled ? "crit" : data?.health?.blocked ? "crit" : "good"}`}>
          {!data?.enabled ? "login not set up" : data?.health?.blocked ? "login blocked" : "login ok"}
        </span>
        <span className="right">
          <button className={`btn btn-sm ${paused ? "btn-brand" : "btn-ghost"}`}
                  disabled={busy} onClick={togglePause}
                  title="Master switch — the background service honors it within a minute">
            {paused ? "Resume collection" : "Pause collection"}
          </button>
        </span>
      </div>

      {!data?.enabled && (
        <div className="banner-crit" style={{ margin: "8px 0" }}>
          <b>Not set up.</b> Add <code>FB_EMAIL / FB_PASSWORD</code> (or
          <code> FB_C_USER / FB_XS</code>) to .env on the server, then restart the dashboard.
        </div>
      )}
      <FbHealthBanner health={data?.health} onAction={healthAction} busy={busy} />

      <div className="kv"><span>Login account</span>
        <b>{ses.identity || "not set"} {ses.method ? `(${ses.method})` : ""}</b></div>
      <div className="kv"><span>Saved session (fb_state.json)</span>
        <b className={ses.state_saved ? "st-good" : "st-warn"}>
          {ses.state_saved ? "present" : "none — will log in fresh"}</b></div>
      <div className="kv"><span>Bandwidth</span>
        <b>server IP{cfg.use_proxy ? " + proxy" : ""}, cap {cfg.monthly_cap_gb} GB/month</b></div>

      <div className="filters" style={{ marginTop: 12, marginBottom: 6 }}>
        <label className="fpill">
          <span>Collection mode</span>
          <select value={f.mode}
                  onChange={(e) => setForm({ ...f, mode: e.target.value })}>
            <option value="pages">Pages (visit each page on its cadence)</option>
            <option value="favorites">Favorites feed (one richer pass)</option>
          </select>
        </label>
        <label className="fpill">
          <span>Default page cadence</span>
          <select value={f.default_interval_s}
                  onChange={(e) => setForm({ ...f, default_interval_s: e.target.value })}>
            {INTERVAL_OPTS.filter(([v]) => Number(v) >= 3600).map(([v, t]) => (
              <option key={v} value={v}>{t}</option>
            ))}
          </select>
        </label>
        <label className="fpill">
          <span>Favorites cadence</span>
          <select value={f.fav_interval_s}
                  onChange={(e) => setForm({ ...f, fav_interval_s: e.target.value })}>
            {INTERVAL_OPTS.map(([v, t]) => (
              <option key={v} value={v}>{t}</option>
            ))}
          </select>
        </label>
        <button className="btn btn-brand btn-sm" disabled={busy} onClick={save}>
          Save configuration
        </button>
      </div>
      {note && (
        <div className={note.startsWith("✓") ? "st-good" : "st-crit"}
             style={{ fontSize: 12.5, fontWeight: 600 }}>{note}</div>
      )}
      <details className="help">
        <summary>How Facebook collection works</summary>
        <p>
          Facebook runs on the server's own bandwidth with a monthly cap — it
          checks each page a few times a day, newest posts only. Credentials
          stay in .env on the server; everything operational is on this panel.
        </p>
        <p>
          <b>Favorites feed (richer):</b> add your pages to the collector
          account's Favorites (Facebook → Feeds → Favourites → Manage, up to
          30), then favorites mode reads them all as one real feed — more
          posts and reaction counts in a single pass.
        </p>
      </details>
    </div>
  );
}

const IG_INTERVALS = [
  ["120", "2 minutes"], ["300", "5 minutes"], ["600", "10 minutes"],
  ["1800", "30 minutes"], ["3600", "1 hour"],
];

function IgSettings({ data, reload }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const accounts = data?.accounts || [];
  const paused = !!data?.paused;
  const checkpointed = accounts.filter((a) => a.checkpoint_at);
  const interval = String(data?.config?.interval_s || 120);

  const togglePause = async () => {
    setBusy(true); setNote("");
    try { await api.igControl(paused ? "resume" : "pause"); reload(); }
    catch (e) { setNote(`✗ ${String(e.message || e)}`); }
    finally { setBusy(false); }
  };
  const setInterval = async (v) => {
    setBusy(true); setNote("");
    try {
      await api.igSettings({ interval_s: v });
      setNote("✓ Saved — applies from the service's next cycle (no restart)");
      reload();
    } catch (e) { setNote(`✗ ${String(e.message || e)}`); }
    finally { setBusy(false); }
  };

  return (
    <div className="panel">
      <div className="phead" style={{ alignItems: "center", flexWrap: "wrap", rowGap: 8 }}>
        <h3><span className="badge platform-ig" style={{ marginRight: 8 }}>IG</span>Instagram network</h3>
        <span className={`chip ${paused ? "warn" : "good"}`}>{paused ? "paused" : "collecting"}</span>
        <span className={`chip ${checkpointed.length ? "crit"
          : accounts.some((a) => a.active) ? "good" : "warn"}`}>
          {checkpointed.length ? "checkpoint — needs a human"
            : accounts.some((a) => a.active) ? "session active" : "no active session"}
        </span>
        <span className="right">
          <button className={`btn btn-sm ${paused ? "btn-brand" : "btn-ghost"}`}
                  disabled={busy} onClick={togglePause}
                  title="Master switch — the background service honors it within a minute">
            {paused ? "Resume collection" : "Pause collection"}
          </button>
        </span>
      </div>

      {checkpointed.map((a) => (
        <div key={a.username}
             style={{ border: "1px solid var(--critical)", borderRadius: 10,
                      padding: "10px 12px", margin: "8px 0",
                      background: "color-mix(in srgb, var(--critical) 8%, transparent)" }}>
          <b className="st-crit">@{a.username} is checkpoint-locked — automatic
            relogins are stopped (since {a.checkpoint_at})</b>
          <div style={{ fontSize: 12.5, marginTop: 4, lineHeight: 1.5 }}>
            No code can clear this; retrying makes the lock stickier. A human
            must: log in as @{a.username} on instagram.com or the app, complete
            the “confirm it's you” check, copy a fresh <code>sessionid</code>
            cookie from that same browser, then on the server run{" "}
            <code>python3 ig_import.py "&lt;sessionid&gt;"</code>. A successful
            import clears this banner by itself.
          </div>
        </div>
      ))}

      {accounts.map((a) => (
        <div className="kv" key={a.username}>
          <span>@{a.username}</span>
          <b className={a.active ? "st-good" : "st-crit"}>
            {a.active ? "active" : "inactive"}{a.proxy ? " · proxied" : ""}
            {a.error ? ` · ${a.error}` : ""}
          </b>
        </div>
      ))}
      {accounts.length === 0 && (
        <div style={{ color: "var(--ink-3)", fontSize: 13, marginTop: 6 }}>
          No Instagram account onboarded yet — accounts are managed on the
          Accounts &amp; Sessions page.
        </div>
      )}

      <div className="filters" style={{ marginTop: 12, marginBottom: 0 }}>
        <label className="fpill">
          <span>Check every</span>
          <select value={interval} disabled={busy}
                  onChange={(e) => setInterval(e.target.value)}>
            {IG_INTERVALS.map(([v, t]) => (
              <option key={v} value={v}>{t}</option>
            ))}
          </select>
        </label>
        {note && (
          <span className={note.startsWith("✓") ? "st-good" : "st-crit"}
                style={{ fontSize: 12.5, fontWeight: 600 }}>{note}</span>
        )}
      </div>
    </div>
  );
}

function StreamsManager({ pid }) {
  const { data, error, reload } = useApi(() => api.streamAssignments(), []);
  const [pick, setPick] = useState("");
  if (error) return null;
  const streams = data?.streams || [];
  const mine = streams.filter((s) => s.projects.includes(pid));
  const others = streams.filter((s) => !s.projects.includes(pid));

  return (
    <div className="panel">
      <div className="phead">
        <h3>Streams in this project</h3>
        <span className="right">what actually feeds this project's feed &amp; delivery</span>
      </div>
      {mine.map((s) => (
        <div className="wl-row" key={s.stream_id}>
          <div className="who">
            <b style={{ overflowWrap: "anywhere" }}>{s.label}</b>
            <small>
              {s.tweets.toLocaleString()} collected
              {s.paused ? " · paused" : ""}
              {s.projects.length > 1 ? ` · in ${s.projects.length} projects` : ""}
            </small>
          </div>
          <div className="right">
            <button className="btn btn-ghost btn-sm"
                    onClick={async () => { await api.detachStream(pid, s.stream_id); reload(); }}>
              Remove from project
            </button>
          </div>
        </div>
      ))}
      {mine.length === 0 && <div className="kv"><span>Nothing attached</span><b /></div>}
      {others.length > 0 && (
        <div className="filters" style={{ marginTop: 12, marginBottom: 0 }}>
          <select value={pick} onChange={(e) => setPick(e.target.value)} style={{ flex: 1 }}>
            <option value="">Attach an existing stream…</option>
            {others.map((s) => (
              <option key={s.stream_id} value={s.stream_id}>
                {s.label} ({s.tweets.toLocaleString()} collected)
              </option>
            ))}
          </select>
          <button className="btn btn-brand btn-sm" disabled={!pick}
                  onClick={async () => { await api.attachStream(pid, Number(pick)); setPick(""); reload(); }}>
            Attach
          </button>
        </div>
      )}
      <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 10 }}>
        Removing a stream only changes what this project shows and delivers —
        the stream, its collection, and its posts stay.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The view: tabs + master-detail
// ---------------------------------------------------------------------------

export default function Watchlists({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const wls = useApi(
    () => (pid ? api.watchlists(pid) : Promise.resolve({ watchlists: [] })),
    [pid],
  );
  const fb = useApi(() => api.fbStatus(pid), [pid], { every: 30_000 });
  const ig = useApi(() => api.igStatus(), [], { every: 60_000 });
  const [tab, setTab] = useState("lists");
  const [sel, setSel] = useState(null);        // "x:<id>" | "fb" | "ig"
  const [creating, setCreating] = useState(false);

  const xLists = wls.data?.watchlists || [];
  const items = useMemo(() => {
    const out = xLists.map((w) => ({
      id: `x:${w.watchlist_id}`, platform: "x", name: w.name,
      sub: w.kind === "xlist" ? "X List"
        : `${w.members.length} ${w.kind === "keywords" ? "keywords" : "handles"}`,
      live: w.streams.some((s) => !s.paused), w,
    }));
    out.push({
      id: "fb", platform: "fb", name: "Facebook pages",
      sub: `${(fb.data?.sources || []).length} pages · ${fmtN(fb.data?.totals?.posts ?? 0)} collected`,
      live: !!fb.data?.enabled && !fb.data?.paused && !fb.data?.health?.blocked,
    });
    out.push({
      id: "ig", platform: "ig", name: "Instagram sources",
      sub: `${(ig.data?.sources || []).length} sources · ${fmtN(ig.data?.totals?.posts ?? 0)} collected`,
      live: (ig.data?.accounts || []).some((a) => a.active),
    });
    return out;
  }, [xLists, fb.data, ig.data]);

  const selected = items.find((i) => i.id === sel) || items[0] || null;
  const reloadAll = () => { wls.reload(); fb.reload(); ig.reload(); };

  const groups = [["x", "X (Twitter)"], ["fb", "Facebook"], ["ig", "Instagram"]];

  return (
    <>
      <PageHead title="Watchlists" onMenu={onMenu}
                sub={project ? `${project.name} — who this project follows, on every platform` : ""}>
        <button className="btn btn-brand" onClick={() => setCreating(true)}>+ New watchlist</button>
      </PageHead>

      <div className="tabs">
        <button className={`tab ${tab === "lists" ? "sel" : ""}`} onClick={() => setTab("lists")}>
          Watchlists
        </button>
        <button className={`tab ${tab === "settings" ? "sel" : ""}`} onClick={() => setTab("settings")}>
          Network &amp; settings
        </button>
      </div>

      {tab === "lists" && (
        <>
          {wls.loading && !wls.data && <Loading />}
          {wls.error && <ErrorState error={wls.error} retry={wls.reload} />}
          {wls.data && (
            <div className="wl-layout">
              <div className="wl-list">
                {groups.map(([p, label]) => {
                  const rows = items.filter((i) => i.platform === p);
                  if (rows.length === 0) return null;
                  return (
                    <React.Fragment key={p}>
                      <div className="wl-group">{label}</div>
                      {rows.map((i) => (
                        <button key={i.id}
                                className={`wl-item ${selected?.id === i.id ? "sel" : ""}`}
                                onClick={() => setSel(i.id)}>
                          <span className={`badge platform-${i.platform}`}>
                            {{ x: "𝕏", fb: "f", ig: "IG" }[i.platform]}
                          </span>
                          <span className="nm">
                            <b>{i.name}</b>
                            <small>{i.sub}</small>
                          </span>
                          <span className={`dot${i.live ? "" : " off"}`} />
                        </button>
                      ))}
                    </React.Fragment>
                  );
                })}
                {items.length === 0 && (
                  <div style={{ color: "var(--ink-3)", fontSize: 13, padding: 14 }}>
                    Nothing yet — “+ New watchlist”.
                  </div>
                )}
              </div>

              <div style={{ minWidth: 0 }}>
                {!selected && (
                  <Empty title="No watchlists in this project yet">
                    A watchlist is a set of accounts to collect. “+ New watchlist”
                    works for X, Facebook, and Instagram alike.
                  </Empty>
                )}
                {selected?.platform === "x" && (
                  <XDetail w={selected.w} onChanged={reloadAll} />
                )}
                {selected?.id === "fb" && (
                  <FbDetail pid={pid} data={fb.data} reload={fb.reload}
                            gotoSettings={() => setTab("settings")} />
                )}
                {selected?.id === "ig" && (
                  <IgDetail pid={pid} data={ig.data} reload={ig.reload}
                            gotoSettings={() => setTab("settings")} />
                )}
              </div>
            </div>
          )}
        </>
      )}

      {tab === "settings" && (
        <>
          <FbSettings data={fb.data} reload={fb.reload} />
          <IgSettings data={ig.data} reload={ig.reload} />
          {pid && <StreamsManager pid={pid} />}
        </>
      )}

      {creating && pid && (
        <AddModal pid={pid} onDone={reloadAll} onClose={() => setCreating(false)} />
      )}
    </>
  );
}
