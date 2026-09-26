// Watchlists — ONE structure for every platform.
import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, byName, fmtAgo, fmtN, fmtPosted, sortBy, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import { Empty, ErrorState, Hint, Loading, Modal, PillSelect, Sec, Segmented, icons, toast, useMediaQuery } from "../components/ui.jsx";
import { cleanHandle, cleanId, parseIgIds } from "../lib/parseIgIds.js";

// Must match FB_SPEEDS in web.py — the named cadences a page can be checked at.
const FB_SPEEDS = { "1h": "1 hour", "3h": "3 hours", "6h": "6 hours",
                    "12h": "12 hours", "24h": "24 hours" };

const INTERVAL_OPTS = [
  ["900", "15 minutes"], ["1800", "30 minutes"], ["3600", "1 hour"],
  ["10800", "3 hours"], ["21600", "6 hours"], ["43200", "12 hours"],
  ["86400", "24 hours"],
];

// Split keyword input into rules on commas and newlines — but NOT on a comma
const splitKeywordRules = (raw) => {
  const out = [];
  let buf = "", quoted = false;
  for (const ch of String(raw || "")) {
    if (ch === '"') { quoted = !quoted; buf += ch; continue; }
    if (!quoted && (ch === "," || ch === "\n")) { out.push(buf); buf = ""; continue; }
    buf += ch;
  }
  out.push(buf);
  return out.map((s) => s.trim()).filter(Boolean);
};

const splitAdd = (kind, raw) =>
  kind === "keywords"
    ? splitKeywordRules(raw)
    : raw.split(/[\s,]+/).filter(Boolean);

// What each platform's watchlist can be — drives the Add modal.
const PLATFORM_KINDS = {
  x: [
    ["query", "Handles (built here — no X List needed)"],
    ["keywords", "Keywords (comma = any, AND = both required)"],
    ["xlist", "Existing X List (fastest polling)"],
    ["links", "Post links (from a Google Sheet, or pasted — re-fetched daily)"],
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

// The unified Add modal — platform first, then the platform's own form.

const KIND_SHORT = {
  query: "Handles", keywords: "Keywords", xlist: "X List", links: "Post links",
  pages: "Pages", favorites: "Favorites feed", user: "Users", hashtag: "Hashtags", following: "Home feed",
};
const KIND_DESC = {
  query: "A set of accounts, built here — no X List needed.",
  keywords: "A search: every post matching the rules, from anyone.",
  xlist: "An existing X List — the fastest polling.",
  links: "Specific posts, pasted or from a Google Sheet, re-fetched daily for fresh numbers.",
  pages: "Each Facebook page is checked on its own cadence.",
  favorites: "One richer pass over the collector account's Favourites feed.",
  user: "A profile's posts — numeric id preferred.",
  hashtag: "Posts under a hashtag.",
  following: "The account's whole home feed — everything it follows. One source is created.",
};

function Field({ label, hint, optional, children }) {
  return (
    <div className="field">
      <div className="flabel">
        <label>{label}{optional ? <span style={{ fontWeight: 500, textTransform: "none", letterSpacing: 0 }}> · optional</span> : null}</label>
        {hint && <Hint text={hint} />}
      </div>
      {children}
    </div>
  );
}

function AddModal({ pid, onDone, onClose }) {
  const [platform, setPlatform] = useState("x");
  const [kind, setKind] = useState("query");
  const [name, setName] = useState("");
  const [listId, setListId] = useState("");
  const [owner, setOwner] = useState("");
  const [handles, setHandles] = useState("");
  const [igValue, setIgValue] = useState("");
  const [sheet, setSheet] = useState("");
  const [synced, setSynced] = useState(null);   // links+sheet: the first sync's report
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const pick = (p) => { setPlatform(p); setKind(PLATFORM_KINDS[p][0][0]); setErr(""); };

  const create = async () => {
    setBusy(true); setErr("");
    try {
      if (platform === "x" && kind === "links") {
        // A sheet names its own watchlists (one per tab) and is read on this
        const body = { project: pid, kind: "links", name, links: handles };
        if (sheet.trim()) body.sheet = sheet.trim();
        const made = await api.createWatchlist(body);
        if (made.warning) { setErr(made.warning); return; }
        if (body.sheet) { setSynced(made); onDone(platform); return; }
      } else if (platform === "x") {
        const body = { project: pid, name, kind };
        if (kind === "xlist") { body.list_id = listId; body.owner_handle = owner; }
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
        if (kind === "following") {
          await api.igSource({ action: "add", label: "home", type: "following", value: "", project: pid });
        } else {
          // Bulk: one source per line/word — paste many at once (like Facebook).
          const items = handles.split(/[\s,]+/).filter(Boolean);
          for (const it of items) {
            const v = it.replace(/^[@#]/, "");
            await api.igSource({ action: "add", label: v, type: kind, value: v, project: pid });
          }
        }
      }
      onDone(platform); onClose();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const canCreate =
    platform === "x" && kind === "links" ? (sheet.trim() || (name.trim() && handles.trim()))
      : platform === "x" ? name.trim() && (kind === "xlist" ? listId.trim() : true)
      : platform === "fb" ? (kind === "favorites" || handles.trim())
      : (kind === "following" || handles.trim());

  if (synced) {
    const wl = synced.watchlists || [];
    return (
      <Modal title="Sheet connected" onClose={onClose}
             sub={synced.sheet?.title ? `“${synced.sheet.title}”` : "The sheet was read once now; it is re-read every 10 minutes."}>
        {synced.error ? (
          <div className="err">{synced.error}</div>
        ) : (
          <div style={{ fontSize: 13.5, lineHeight: 1.6 }}>
            <b>{synced.tabs}</b> tab{synced.tabs === 1 ? "" : "s"} → <b>{wl.length}</b> watchlist{wl.length === 1 ? "" : "s"}
            {" · "}<b>{synced.found}</b> post link{synced.found === 1 ? "" : "s"} found
            {synced.added ? <> · <b>{synced.added}</b> new</> : null}
            {synced.skipped ? <> · {synced.skipped} skipped (not X post links)</> : null}
            {synced.tco_unresolved ? <> · {synced.tco_unresolved} t.co link{synced.tco_unresolved === 1 ? "" : "s"} could not be resolved</> : null}
            <ul style={{ margin: "8px 0 0", paddingLeft: 18, color: "var(--ink-2)" }}>
              {wl.map((t) => (
                <li key={t.watchlist_id}>{t.name}{t.tab !== t.name ? ` (tab “${t.tab}”)` : ""} — {t.found} link{t.found === 1 ? "" : "s"}</li>
              ))}
            </ul>
            <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 10 }}>
              New rows are picked up on the next sync and fetched within a couple of
              minutes. A link removed from the sheet is marked, never deleted.
            </div>
          </div>
        )}
        <div className="row">
          <button className="btn btn-brand" onClick={onClose}>Done</button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="New watchlist" onClose={onClose}
           sub="Pick a platform and a type — the fields below adapt.">
      <div className="field">
        <label>Platform</label>
        <Segmented value={platform} onChange={pick}
                   options={[["x", "𝕏  X (Twitter)"], ["fb", "Facebook"], ["ig", "Instagram"]]} />
      </div>
      <div className="field">
        <label>Type</label>
        <Segmented value={kind} onChange={setKind}
                   options={PLATFORM_KINDS[platform].map(([v, t]) => [v, KIND_SHORT[v] || t, t])} />
        <div className="fhint">{KIND_DESC[kind]}</div>
      </div>

      {platform === "x" && kind === "links" && (
        <>
          <Field label="Google Sheet URL" optional
                 hint="Share the sheet with the collector's service account as Viewer (the same account Sheet delivery uses). Every cell of every tab is scanned for x.com post links and every tab becomes a watchlist named after it. Re-read every 10 minutes.">
            <input value={sheet} autoFocus onChange={(e) => setSheet(e.target.value)}
                   placeholder="https://docs.google.com/spreadsheets/d/…/edit" />
          </Field>
          {!sheet.trim() && (
            <>
              <Field label="Name">
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Launch week posts" />
              </Field>
              <Field label="Post links" hint="One x.com post link per line. Each post is re-fetched on the list's cadence (12h/24h/48h) and its likes, reposts, replies, quotes, views and bookmarks are overwritten with the latest numbers. Use a project that is not bound to Watch-Tower.">
                <textarea rows="5" value={handles} onChange={(e) => setHandles(e.target.value)}
                          placeholder={"https://x.com/nasa/status/1789…\nhttps://x.com/isro/status/1790…"} />
              </Field>
            </>
          )}
        </>
      )}

      {platform === "x" && kind !== "links" && (
        <>
          <Field label="Name">
            <input value={name} autoFocus onChange={(e) => setName(e.target.value)} placeholder="e.g. Cabinet" />
          </Field>
          {kind === "xlist" ? (
            <>
              <Field label="X List URL or id">
                <input value={listId} onChange={(e) => setListId(e.target.value)}
                       placeholder="https://x.com/i/lists/1234567890123456789" />
              </Field>
              <Field label="Owner" optional
                     hint="The X account this list was made on. A list lives on x.com and only its owner can add or remove members — recording it here is how anyone later knows which account to sign in as.">
                <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="@our_scraper_2" />
              </Field>
            </>
          ) : kind === "keywords" ? (
            <Field label="Keyword rules"
                   hint={'One rule per line or separated by commas. Comma / new line = OR (match any). Uppercase AND between two words = both required, any order (Varanasi AND Modi). Two words with no operator also mean both, so Devendra Fadnavis also matches "Fadnavis … Devendra"; quote for the exact phrase: "Devendra Fadnavis". Also -exclude, #hashtag, @mention.'}>
              <textarea rows="5" value={handles} onChange={(e) => setHandles(e.target.value)}
                        placeholder={'Devendra Fadnavis, \u0926\u0947\u0935\u0947\u0902\u0926\u094d\u0930 \u092b\u0921\u0923\u0935\u0940\u0938\nCM AND maharashtra\n"input tax credit", #Chhattisgarh'} />
              <div className="fhint">comma = any · <code>AND</code> = both · <code>"quotes"</code> = exact phrase · <code>-word</code> = exclude</div>
            </Field>
          ) : (
            <Field label="Handles" hint="One per line, @ optional. A profile URL works too.">
              <textarea rows="5" value={handles} onChange={(e) => setHandles(e.target.value)}
                        placeholder={"@DrKirodilalBJP\nJoraramKumawat\nhttps://x.com/KirodiOffice"} />
            </Field>
          )}
        </>
      )}

      {platform === "fb" && (
        <Field label="Page handles"
               hint={"From the page URL, one per line." + (kind === "favorites"
                 ? " Favorites mode switches collection to the account's Favourites feed — one richer pass instead of page-by-page checks. Add the pages here so posts are attributed to them, and add them to Favourites in the collector's Facebook account (Feeds → Favourites → Manage)."
                 : "")}>
          <textarea rows="4" value={handles} onChange={(e) => setHandles(e.target.value)}
                    placeholder={"narendramodi\nAmitShahOfficial"} />
        </Field>
      )}

      {platform === "ig" && kind !== "following" && (
        <Field label={kind === "user" ? "Usernames or numeric ids" : "Hashtags"}
               hint={kind === "user"
                 ? "One per line. A username works; a numeric id is more robust when the session is restricted (find it in the profile source as “profile_id”). The label is the username/id itself."
                 : "Without the #, one per line."}>
          <textarea rows="5" value={handles} autoFocus onChange={(e) => setHandles(e.target.value)}
                    placeholder={kind === "user" ? "natgeo\nnasa\n787132" : "wildlife\nnature"} />
        </Field>
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

// X detail panel

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
  useEffect(() => { setF(w.filters || {}); }, [w.watchlist_id]);

  const active = Object.keys(w.filters || {}).filter((k) => w.filters[k]).length;
  const dirty = JSON.stringify(f) !== JSON.stringify(w.filters || {});
  const save = async () => {
    setBusy(true);
    try { await api.watchlistFilters(w.watchlist_id, f); onChanged(); }
    catch { /* toast */ }
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
    <Sec label="Collection filters"
         hint={w.kind === "xlist"
           ? "Applied at collection time: the List timeline is read as usual and filtered posts are dropped before they are stored, so they never reach the feed, exports or Telegram. Posts already collected stay."
           : "Applied at collection time: filtered posts are never fetched at all. Posts already collected stay."}
         right={<>
           {active > 0 && <span className="chip">{active} active</span>}
           <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)} aria-expanded={open}>
             {open ? "Hide" : active ? "Edit" : "Add filters"}
           </button>
         </>}>
      <div className={`reveal${open ? " open" : ""}`}>
        <div>
          <div className="filters" style={{ marginBottom: 8 }}>
            {FILTER_BOXES.map(([k, l]) => box(k, l))}
          </div>
          <div className="filters" style={{ marginBottom: 0 }}>
            <input placeholder="language (hi, en…)" value={f.lang || ""} style={{ width: 150 }}
                   title="Only posts X tags with this language code"
                   onChange={(e) => setF((s) => ({ ...s, lang: e.target.value }))} />
            <input placeholder="min likes" inputMode="numeric" value={f.min_likes || ""} style={{ width: 110 }}
                   onChange={(e) => setF((s) => ({ ...s, min_likes: e.target.value }))} />
            <input placeholder="min retweets" inputMode="numeric" value={f.min_retweets || ""} style={{ width: 120 }}
                   onChange={(e) => setF((s) => ({ ...s, min_retweets: e.target.value }))} />
            <button className="btn btn-brand btn-sm" disabled={busy || !dirty} onClick={save}>
              {busy ? "Saving…" : "Save filters"}
            </button>
          </div>
        </div>
      </div>
    </Sec>
  );
}

// Rename happens in a modal so the panel header never changes shape.
function RenameModal({ w, onChanged, onClose }) {
  const [val, setVal] = useState(w.name || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const save = async () => {
    const name = val.trim();
    if (!name || name === w.name) { onClose(); return; }
    setBusy(true); setErr("");
    try {
      const r = await api.renameWatchlist(w.watchlist_id, name);
      if (r && r.error) { setErr(r.error); return; }
      onChanged(); onClose();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };
  return (
    <Modal title="Rename watchlist" sub="Display name only — collection is unaffected." onClose={onClose}>
      <div className="field">
        <label htmlFor="wlname">Name</label>
        <input id="wlname" value={val} autoFocus maxLength={120}
               onChange={(e) => setVal(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && save()} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !val.trim()} onClick={save}>Save</button>
      </div>
    </Modal>
  );
}

// Who owns this X List — only that account can edit its members on x.com.
function OwnerModal({ w, onChanged, onClose }) {
  const [val, setVal] = useState(w.owner_handle || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const save = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api.watchlistOwner(w.watchlist_id, val.trim());
      if (r && r.error) { setErr(r.error); return; }
      onChanged(); onClose();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };
  return (
    <Modal title="List owner" sub="The X account that can edit this List's members on x.com." onClose={onClose}>
      <div className="field">
        <label htmlFor="wlowner">Handle</label>
        <input id="wlowner" value={val} autoFocus placeholder="@handle — blank to clear"
               onChange={(e) => setVal(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && save()} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy} onClick={save}>Save</button>
      </div>
    </Modal>
  );
}

// The accounts inside an X List — cached; "Refresh members" pulls them from X.
function XListMembers({ listId }) {
  const { data, reload } = useApi(() => api.xlistMembers(listId), [listId]);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const all = data?.members || [];
  const members = sortBy(all, "username").filter((m) =>
    !q || `${m.display_name || ""} ${m.username}`.toLowerCase().includes(q.toLowerCase()));

  const refresh = async () => {
    setBusy(true);
    try { await api.refreshXlistMembers(listId); reload(); }
    catch { /* toast */ }
    finally { setBusy(false); }
  };

  return (
    <Sec label="Members"
         hint="The List is collected as one fast stream. Refreshing pulls the individual accounts inside it from X (spends a little X budget; cached afterwards)."
         right={<>
           {all.length > 0 && <span className="chip">{fmtN(all.length)} accounts{data?.fetched_ms ? ` · ${fmtAgo(data.fetched_ms)}` : ""}</span>}
           {all.length > 8 && <input className="mini" value={q} placeholder="search…" onChange={(e) => setQ(e.target.value)} />}
           <button className="btn btn-ghost btn-sm" disabled={busy} onClick={refresh}>
             {busy ? "Fetching…" : all.length ? "Refresh" : "Fetch members"}
           </button>
         </>}>
      <div className="members-box tall">
        {members.map((m) => (
          <div className="wl-row" key={m.user_id}>
            <div className="who" style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {m.avatar
                ? <img src={m.avatar} alt="" width="30" height="30" style={{ borderRadius: "50%", flex: "none" }} />
                : <span className="pfp" style={{ background: "var(--brand)", width: 30, height: 30, fontSize: 12 }}>
                    {(m.display_name || m.username || "?").slice(0, 1).toUpperCase()}
                  </span>}
              <div style={{ minWidth: 0 }}>
                <b>{m.display_name || m.username}</b>
                <small style={{ display: "block", color: "var(--ink-3)" }}>@{m.username}</small>
              </div>
            </div>
            <a className="right" href={`https://x.com/${m.username}`} target="_blank"
               rel="noreferrer" style={{ color: "var(--ink-3)", fontSize: 12 }}>open ↗</a>
          </div>
        ))}
        {all.length === 0 && <div className="muted">Members not fetched yet.</div>}
        {all.length > 0 && members.length === 0 && <div className="muted">no match for “{q}”</div>}
      </div>
    </Sec>
  );
}

const DEPTH_OPTS = [["", "default"], ["1", "1 page (~20)"], ["3", "3 pages (~60)"], ["5", "5 pages (~100)"], ["10", "10 pages (~200)"], ["25", "25 pages (~500)"]];
const DIG_OPTS = [["", "off"], ["300", "every 5 min"], ["600", "every 10 min"], ["900", "every 15 min"]];
const EVERY_OPTS = [["", "auto"], ["300", "5 min"], ["600", "10 min"], ["900", "15 min"], ["1800", "30 min"], ["3600", "1 hour"]];

// Depth and history — the two controls that answer "why has this stopped?".
function DepthRow({ w, onChanged }) {
  const [busy, setBusy] = useState(false);
  const bf = w.backfill || {};
  const run = async (fn) => {
    setBusy(true);
    try { const r = await fn(); if (!(r && r.error)) onChanged(); }
    catch { /* toast */ }
    finally { setBusy(false); }
  };

  return (
    <div className="ctrl-row">
      <PillSelect label="depth" value={w.pages ? String(w.pages) : ""} options={DEPTH_OPTS} disabled={busy}
                  title={"How far down the timeline ONE check may go, in pages of ~20 posts. Raise it if a busy watchlist misses posts between checks. It does not help a watchlist that has stopped growing — use 'dig older' for that."}
                  onChange={(v) => run(() => api.watchlistDepth(w.watchlist_id, v))} />
      <PillSelect label="dig older" value={bf.auto ? String(Math.round(bf.every_s || 300)) : ""} options={DIG_OPTS}
                  disabled={busy || !w.streams.length}
                  title={"Keep walking BACKWARDS through this query on a schedule, collecting older posts until X has no more. For archival queries or accounts that stopped posting. Takes the smaller share of the rate limit, resumes across restarts, and switches itself off when the archive is empty."}
                  onChange={(v) => run(() => v ? api.watchlistBackfillAuto(w.watchlist_id, true, v)
                                              : api.watchlistBackfillAuto(w.watchlist_id, false))} />
      <button className="btn btn-ghost btn-sm" disabled={busy || !w.streams.length}
              title="Check for NEW posts right now instead of waiting for the next scheduled check. One page per stream."
              onClick={() => run(async () => {
                const r = await api.watchlistFetchNow(w.watchlist_id);
                if (r && r.needs_ack && confirm("The rate-limit guard has warnings. Fetch anyway?"))
                  return api.watchlistFetchNow(w.watchlist_id, true);
                return r;
              })}>
        {busy ? "…" : "Fetch now"}
      </button>
      {bf.auto && !bf.exhausted && (
        <span className="chip good" title="The backwards sweep is running in the background">
          <span className="dot pulse" /> digging · {fmtN(bf.got || 0)} so far
        </span>
      )}
      {bf.exhausted && bf.walked > 0 && (
        <span className="chip" title="X returned no further results for this query">
          history complete · {fmtN(bf.got || 0)} older
        </span>
      )}
    </div>
  );
}

// Shared watchlists — one list, several projects.

const KIND_LABEL = { xlist: "X List", keywords: "keywords", links: "links", query: "handles" };

// "shared · created in A · also used by B" — only when there is something to say.
function SharedLine({ w, pid }) {
  const others = (w.projects || []).filter((p) => p.project_id !== pid);
  const mine = w.owner_project_id === pid;
  if (mine && others.length === 0) return null;
  const names = sortBy(others).map((p) => p.name).join(", ");
  return (
    <span className="chip" title={"Used by more than one project. Members, filters and cadence are shared — a change here changes it everywhere."
      + (mine ? ` Also used by ${names}.` : ` Created in ${w.owner_project || `project #${w.owner_project_id}`}.`)}>
      shared · {mine ? `also in ${names}` : `from ${w.owner_project || `#${w.owner_project_id}`}`}
    </span>
  );
}

// Delete vs remove. From the owner: delete (refused by the server while
function WatchlistDeleteModal({ w, pid, onClose, onChanged, sub }) {
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const mine = !w.owner_project_id || w.owner_project_id === pid;
  const others = (w.projects || []).filter((p) => p.project_id !== pid).map((p) => p.name);
  const go = async () => {
    setBusy(true); setErr("");
    try {
      const r = mine ? await api.removeWatchlist(w.watchlist_id, pid)
                     : await api.detachWatchlist(pid, w.watchlist_id);
      if (r?.error) { setErr(r.error); return; }
      onClose(); onChanged();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };
  if (!mine) {
    return (
      <Modal title={`Remove “${w.name}” from this project?`} onClose={onClose}
             sub={`It was created in ${w.owner_project || "another project"} and keeps collecting there. Only this project stops seeing its posts — nothing is deleted.`}>
        {err && <div className="err">{err}</div>}
        <div className="row">
          <button className="btn btn-ghost" onClick={onClose}>Keep it</button>
          <button className="btn btn-danger" disabled={busy} onClick={go}>Remove from project</button>
        </div>
      </Modal>
    );
  }
  return (
    <Modal title={`Delete “${w.name}”?`} onClose={onClose} sub={sub}>
      {others.length > 0 && (
        <div className="err" style={{ marginTop: 10 }}>
          Also used by <b>{others.join(", ")}</b>. Remove it from those projects first —
          or leave it, and it keeps collecting for them.
        </div>
      )}
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Keep it</button>
        <button className="btn btn-danger" disabled={busy || others.length > 0} onClick={go}>Delete</button>
      </div>
    </Modal>
  );
}

// The picker: every X watchlist in every other project, grouped by the
function AddExistingModal({ pid, onDone, onClose }) {
  const lib = useApi(() => api.watchlistLibrary(pid), [pid]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(null);     // watchlist_id being added
  const [err, setErr] = useState("");
  const [added, setAdded] = useState(() => new Set());

  const rows = sortBy((lib.data?.watchlists || []).filter((w) => w.project_id !== pid));
  const needle = q.trim().toLowerCase();
  const shown = needle
    ? rows.filter((w) => `${w.name} ${w.owner_project} ${KIND_LABEL[w.kind] || w.kind}`.toLowerCase().includes(needle))
    : rows;
  const groups = [];
  for (const w of shown) {
    let g = groups.find((x) => x.project_id === w.project_id);
    if (!g) { g = { project_id: w.project_id, name: w.owner_project, archived: !!w.owner_archived, rows: [] }; groups.push(g); }
    g.rows.push(w);
  }
  groups.sort(byName());

  const add = async (w) => {
    setBusy(w.watchlist_id); setErr("");
    try {
      const r = await api.attachWatchlist(pid, w.watchlist_id);
      if (r?.error) { setErr(r.error); return; }
      setAdded((s) => new Set([...s, w.watchlist_id]));
      onDone();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(null); }
  };

  const size = (w) => w.kind === "xlist" ? "X List"
    : w.kind === "links" ? `${fmtN(w.links)} link${w.links === 1 ? "" : "s"}`
    : `${fmtN(w.members)} ${w.kind === "keywords" ? "keyword" : "handle"}${w.members === 1 ? "" : "s"}`;

  return (
    <Modal title="Add an existing watchlist"
           sub="Lists created in other projects. Adding one shares it — the same list, collected once, and every post it has ever collected shows here too."
           onClose={onClose}>
      {lib.loading && !lib.data && <Loading />}
      {lib.error && <ErrorState error={lib.error} retry={lib.reload} />}
      {lib.data && rows.length === 0 && (
        <Empty title="No watchlists in other projects yet">
          Create one in any project and it can be added here.
        </Empty>
      )}
      {rows.length > 8 && (
        <div className="field" style={{ marginTop: 12 }}>
          <input value={q} placeholder={`filter ${rows.length} watchlists…`} autoFocus
                 onChange={(e) => setQ(e.target.value)} />
        </div>
      )}
      <div className="proj-manage" style={{ maxHeight: "55vh", overflow: "auto" }}>
        {groups.map((g) => (
          <React.Fragment key={g.project_id}>
            <div className="wl-group" style={{ padding: "8px 2px 2px" }}>
              {g.name}{g.archived ? " · archived" : ""}
              <span className="cnt">· {g.rows.length}</span>
            </div>
            {g.rows.map((w) => {
              const have = w.attached || added.has(w.watchlist_id);
              return (
                <div key={w.watchlist_id} className={`proj-row${have ? " archived" : ""}`}>
                  <span className={`dot${w.live ? "" : " off"}`} title={w.live ? "collecting" : "paused"} />
                  <div className="name">
                    <b>{w.name}</b>
                    <small>
                      {KIND_LABEL[w.kind] || w.kind} · {size(w)} · {fmtN(w.tweets)} collected
                      {w.shared && <> · also in {w.projects.filter((p) => !p.owner).map((p) => p.name).join(", ")}</>}
                    </small>
                  </div>
                  <div className="acts">
                    {have
                      ? <span className="chip good">in this project</span>
                      : <button className="btn btn-brand btn-sm" disabled={busy != null}
                                onClick={() => add(w)}>{busy === w.watchlist_id ? "adding…" : "Add"}</button>}
                  </div>
                </div>
              );
            })}
          </React.Fragment>
        ))}
        {needle && shown.length === 0 && (
          <div style={{ color: "var(--ink-3)", fontSize: 12.5, padding: "8px 12px" }}>
            no watchlist matches “{q}”
          </div>
        )}
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
      </div>
    </Modal>
  );
}

function XDetail({ w, pid, onChanged, onBack }) {
  const [adding, setAdding] = useState("");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [owning, setOwning] = useState(false);
  const [editing, setEditing] = useState(null);   // {old, val}
  useEffect(() => { setSearch(""); setAdding(""); }, [w.watchlist_id]);

  const change = async (add, remove) => {
    setBusy(true);
    try { await api.watchlistMembers(w.watchlist_id, add, remove); setAdding(""); onChanged(); }
    catch { /* toast */ }
    finally { setBusy(false); }
  };

  const live = w.streams.filter((s) => !s.paused);
  const collected = w.streams.reduce((a, s) => a + (s.tweets || 0), 0);
  const curInterval = w.interval_s ? String(w.interval_s) : "";
  const sorted = sortBy(w.members, "handle");
  const members = search
    ? sorted.filter((m) => m.handle.toLowerCase().includes(search.toLowerCase()))
    : sorted;

  const [busyPause, setBusyPause] = useState(false);
  const paused = w.streams.length > 0 && w.streams.every((s) => s.paused);
  const togglePause = async () => {
    setBusyPause(true);
    try {
      for (const s of w.streams) await api.streamSettings({ label: s.label, paused: !paused }, { quiet: true });
      toast.ok(paused ? "Collection resumed" : "Collection paused");
      onChanged();
    } catch (e) { toast.err(String(e.message || e)); }
    finally { setBusyPause(false); }
  };
  const foreign = w.owner_project_id && w.owner_project_id !== pid;
  const unit = w.kind === "keywords" ? "keyword rule" : "handle";

  return (
    <div className="panel detail" key={w.watchlist_id}>
      <div className="dhead">
        <div className="dtitle">
          {onBack && <button className="icon-btn back" onClick={onBack} aria-label="All watchlists">{icons.back}</button>}
          <h3 title={w.name}>{w.name}</h3>
          <button className="icon-btn xs" title="Rename" aria-label="Rename" onClick={() => setRenaming(true)}>{icons.edit}</button>
          <span className="badge platform-x">{KIND_LABEL[w.kind] || w.kind}</span>
          {paused && <span className="chip warn">paused</span>}
        </div>
        <div className="dactions">
          <PillSelect label="every" value={curInterval} options={EVERY_OPTS}
                      title={"How often the collector re-checks this watchlist. A chosen interval is exact; 'auto' lets the adaptive controller speed up on busy streams and slow down on quiet ones."}
                      onChange={async (v) => { await api.watchlistInterval(w.watchlist_id, v); onChanged(); }} />
          <button className="btn btn-ghost btn-sm" disabled={busyPause || w.streams.length === 0} onClick={togglePause}
                  title={paused ? "Start checking this watchlist again" : "Stop checking; nothing already collected is lost"}>
            {busyPause ? "…" : paused ? "Resume" : "Pause"}
          </button>
          <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}
                  title={foreign ? "Unlink from this project — the list stays in the project that created it" : "Delete this watchlist"}>
            {foreign ? "Remove" : "Delete"}
          </button>
        </div>
      </div>

      <div className="dmeta">
        {w.kind === "xlist"
          ? <span className="chip" title="Members are managed on x.com">List {w.list_id}</span>
          : <span className="chip">{fmtN(w.members.length)} {unit}{w.members.length === 1 ? "" : "s"}</span>}
        <span className={`chip ${live.length ? "good" : ""}`} title="Compiled streams the collector is polling for this watchlist">
          {live.length} live stream{live.length === 1 ? "" : "s"}
        </span>
        <span className="chip" title="Posts collected through this watchlist so far">{fmtN(collected)} collected</span>
        <SharedLine w={w} pid={pid} />
        {w.kind === "xlist" && (
          <button className="chip as-btn" onClick={() => setOwning(true)}
                  title={w.owner_handle ? "Only this account can edit the List's members on x.com. Click to change." : "Record which X account owns this List. Click to set."}>
            {w.owner_handle ? `owner @${w.owner_handle}` : "owner not set"} <span className="chev">›</span>
          </button>
        )}
      </div>

      <Sec label="Schedule"
           hint="'every' is the check cadence. 'depth' is how far one check reads. 'dig older' walks backwards through history on its own schedule. 'Fetch now' runs one check immediately.">
        <DepthRow w={w} onChanged={onChanged} />
      </Sec>

      {w.kind !== "xlist" && (
        <Sec label={w.kind === "keywords" ? "Keyword rules" : "Handles"}
             hint={w.kind === "keywords"
               ? "Each rule is one search. Inside a rule, a comma means OR (match any); AND means every term must appear. Click a rule to edit it."
               : "Accounts whose posts this watchlist collects. Click a handle to edit it, ✕ to remove."}
             right={w.members.length > 8 && (
               <input className="mini" value={search} placeholder={`search ${w.members.length}…`}
                      onChange={(e) => setSearch(e.target.value)} />
             )}>
          <div className="members-box">
            {members.map((mb) => (
              <span className="tag" key={mb.handle}>
                <button className="tag-txt" title="Click to edit" disabled={busy}
                        onClick={() => setEditing({ old: mb.handle, val: mb.handle })}>
                  {w.kind === "keywords" ? mb.handle : `@${mb.handle}`}
                </button>
                <button aria-label={`remove ${mb.handle}`} title="Remove" disabled={busy}
                        onClick={() => change([], [mb.handle])}>✕</button>
              </span>
            ))}
            {w.members.length === 0 && <div className="muted">Nothing yet — add {w.kind === "keywords" ? "a rule" : "a handle"} below.</div>}
            {w.members.length > 0 && members.length === 0 && <div className="muted">no match for “{search}”</div>}
          </div>
          <div className="add-row">
            <input value={adding}
                   placeholder={w.kind === "keywords" ? "add a rule, e.g. Varanasi AND Modi" : "add @handle or profile URL"}
                   title={w.kind === "keywords"
                     ? "comma = OR (match any) · 'Varanasi AND Modi' = both required · one rule per line"
                     : "@handle, a profile URL, or several separated by spaces"}
                   onChange={(e) => setAdding(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && adding.trim() && change(splitAdd(w.kind, adding), [])} />
            <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()}
                    onClick={() => change(splitAdd(w.kind, adding), [])}>
              Add
            </button>
          </div>
        </Sec>
      )}
      {w.kind === "xlist" && <XListMembers listId={w.list_id} />}

      <FiltersPanel w={w} onChanged={onChanged} />

      {renaming && <RenameModal w={w} onChanged={onChanged} onClose={() => setRenaming(false)} />}
      {owning && <OwnerModal w={w} onChanged={onChanged} onClose={() => setOwning(false)} />}
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
        <WatchlistDeleteModal w={w} pid={pid} onClose={() => setConfirming(false)} onChanged={onChanged}
                              sub="Collection stops. Everything already collected stays in the database." />
      )}
    </div>
  );
}

// Links detail panel — post URLs re-fetched on a cadence (X_LINKS_PLAN.md §7).

const LINK_REFRESH = [["43200", "12 hours"], ["86400", "24 hours"], ["172800", "48 hours"]];
const fmtDay = (iso) => {
  const t = Date.parse(iso + "T00:00:00");
  return t ? new Date(t).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : iso;
};
const LINK_SORTS = [["", "added"], ["views", "views"], ["likes", "likes"],
                    ["posted", "posted"], ["refreshed", "last refreshed"]];
const LINK_STATUS_CHIP = { ok: "good", pending: "", unavailable: "warn", removed: "" };

function LinkStatus({ row }) {
  const cls = LINK_STATUS_CHIP[row.status] ?? "";
  const title = row.status_note || (row.status === "pending"
    ? (row.fail_streak ? `${row.fail_streak} miss${row.fail_streak === 1 ? "" : "es"} — retrying with back-off` : "not fetched yet")
    : row.status === "ok" ? "last fetch returned the post" : "");
  return (
    <span className={`chip ${cls}`} title={title}
          style={row.status === "removed" ? { opacity: 0.6 } : undefined}>
      {row.status}{row.status === "pending" && row.fail_streak ? ` ·${row.fail_streak}` : ""}
    </span>
  );
}

function LinkRow({ row, onRemove, busy, showSection = true }) {
  const handle = row.author_username;
  const text = (row.text || "").replace(/\s+/g, " ").trim();
  return (
    <tr style={row.status === "removed" ? { opacity: 0.55 } : undefined}>
      <td style={{ minWidth: 220, maxWidth: 340 }}>
        <div style={{ display: "flex", gap: 9, alignItems: "flex-start" }}>
          {row.author_avatar
            ? <img src={row.author_avatar} alt="" style={{ width: 26, height: 26, borderRadius: "50%", flex: "none", marginTop: 1 }} />
            : <span className="pfp" style={{ width: 26, height: 26, fontSize: 11, background: "var(--brand)" }}>
                {(handle || "?").slice(0, 1).toUpperCase()}
              </span>}
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13 }}>
              {handle ? <b>@{handle}</b>
                : <span style={{ color: "var(--ink-3)" }}>
                    {row.status === "unavailable" ? "post unavailable" : "not fetched yet"}
                  </span>}
              {row.created_at && <span style={{ color: "var(--ink-3)", marginLeft: 8, fontSize: 12 }}>{fmtPosted(row.created_at)}</span>}
              {row.section && showSection && (
                <span className="badge rt" style={{ marginLeft: 8, fontWeight: 600 }} title="section heading in the sheet">{row.section}</span>
              )}
            </div>
            <div style={{ color: row.fetched ? "var(--ink-2)" : "var(--ink-3)", fontSize: 12.5, lineHeight: 1.4,
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 290 }}
                 title={text || row.status_note || row.url}>
              {text || (row.status === "unavailable" && row.status_note ? row.status_note : row.url)}
            </div>
          </div>
        </div>
      </td>
      <td className="num">{fmtN(row.like_count)}</td>
      <td className="num">{fmtN(row.retweet_count)}</td>
      <td className="num" title={row.quote_count != null ? `${fmtN(row.quote_count)} quotes` : ""}>{fmtN(row.reply_count)}</td>
      <td className="num"><b>{fmtN(row.view_count)}</b></td>
      <td><LinkStatus row={row} /></td>
      <td style={{ whiteSpace: "nowrap", color: "var(--ink-3)", fontSize: 12 }}
          title={row.last_refresh_ms ? new Date(row.last_refresh_ms).toLocaleString() : ""}>
        {row.last_refresh_ms ? fmtAgo(row.last_refresh_ms) : "—"}
        {row.refresh_count > 1 && <span style={{ marginLeft: 4 }}>·{row.refresh_count}</span>}
        {row.force && <span className="chip warn" style={{ marginLeft: 6 }}>queued</span>}
      </td>
      <td style={{ whiteSpace: "nowrap", paddingLeft: 4, paddingRight: 6 }}>
        <a className="btn btn-ghost btn-sm" style={{ padding: "3px 7px" }} href={row.url} target="_blank" rel="noreferrer" title="open on X">↗</a>
        {row.status !== "removed" && (
          <button className="btn btn-ghost btn-sm" disabled={busy} style={{ marginLeft: 3, padding: "3px 7px" }}
                  title="stop refreshing this post (its numbers stay)" onClick={() => onRemove(row)}>✕</button>
        )}
      </td>
    </tr>
  );
}

function LinksDetail({ pid, w, onChanged }) {
  const [sort, setSort] = useState("");
  const [status, setStatus] = useState("");
  const [groupBy, setGroupBy] = useState("");   // "" | "author" | "section"
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [renaming, setRenaming] = useState(false);

  const rows = useApi(
    () => api.links({ project: pid, watchlist: w.watchlist_id, sort, status, limit: 500 }),
    [pid, w.watchlist_id, sort, status], { every: 30_000 },
  );
  const data = rows.data?.rows || [];
  const shown = search
    ? data.filter((r) => `${r.author_username || ""} ${r.text || ""} ${r.url}`.toLowerCase().includes(search.toLowerCase()))
    : data;
  const summ = w.links || {};
  const sheet = w.sheet;
  const paused = !!w.paused;

  const act = async (fn, okMsg) => {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await fn();
      if (okMsg) setMsg(typeof okMsg === "function" ? okMsg(r) : okMsg);
      rows.reload(); onChanged();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const groups = useMemo(() => {
    if (!groupBy) return null;
    const m = new Map();
    for (const r of shown) {
      const k = groupBy === "author"
        ? (r.author_username ? `@${r.author_username}` : "(not fetched yet)")
        : (r.section || "(no section)");
      if (!m.has(k)) m.set(k, { key: k, avatar: groupBy === "author" ? r.author_avatar : null,
                                followers: groupBy === "author" ? r.author_followers : null,
                                rows: [], views: 0, likes: 0 });
      const g = m.get(k);
      g.rows.push(r); g.views += r.view_count || 0; g.likes += r.like_count || 0;
      if (!g.avatar && groupBy === "author" && r.author_avatar) g.avatar = r.author_avatar;
    }
    const out = [...m.values()];
    // Sections keep the sheet's own order (first row wins); authors sort by reach.
    if (groupBy === "author") out.sort((a, b) => b.views - a.views);
    return out;
  }, [groupBy, shown]);
  const hasSections = data.some((r) => r.section);

  const head = (
    <tr>
      <th>Post</th><th className="num" title="likes">❤</th><th className="num" title="reposts">↻</th>
      <th className="num" title="replies">💬</th>
      <th className="num" title="views (reach)">👁</th><th>State</th><th>Read</th><th></th>
    </tr>
  );

  const foreign = w.owner_project_id && w.owner_project_id !== pid;
  const refreshOpts = LINK_REFRESH.some(([v]) => v === String(w.refresh_every_s)) || !w.refresh_every_s
    ? LINK_REFRESH : [...LINK_REFRESH, [String(w.refresh_every_s), `${Math.round(w.refresh_every_s / 3600)} hours`]];
  return (
    <div className="panel detail" key={w.watchlist_id}>
      <div className="dhead">
        <div className="dtitle">
          <h3 title={w.name}>{w.name}</h3>
          <button className="icon-btn xs" title="Rename" aria-label="Rename" onClick={() => setRenaming(true)}>{icons.edit}</button>
          <span className="badge platform-x">links</span>
          {paused && <span className="chip warn">paused</span>}
        </div>
        <div className="dactions">
          <PillSelect label="every" value={String(w.refresh_every_s || 86400)} options={refreshOpts} disabled={busy}
                      title={"How often every post on this list is re-fetched. One request per post per cycle, trickled through the day on its own rate budget — it never slows the handle watchlists."}
                      onChange={(v) => act(() => api.linksInterval(w.watchlist_id, Number(v)))} />
          <button className="btn btn-ghost btn-sm" disabled={busy || !summ.total}
                  title="Queue every post on this list for a fetch on the next pass (within a minute)"
                  onClick={() => act(() => api.linksRefreshNow(w.watchlist_id),
                                     (r) => `${r.queued} post${r.queued === 1 ? "" : "s"} queued — the collector picks them up on its next pass`)}>
            Refresh now
          </button>
          {sheet && (
            <button className="btn btn-ghost btn-sm" disabled={busy}
                    title="Re-read the Google Sheet now (every tab)"
                    onClick={() => act(() => api.syncLinkSheet(sheet.link_sheet_id),
                                       (r) => r.error ? r.error : `sheet read: ${r.found} link${r.found === 1 ? "" : "s"} across ${r.tabs} tab${r.tabs === 1 ? "" : "s"}, ${r.added} new, ${r.removed} gone`)}>
              Sync sheet
            </button>
          )}
          <button className="btn btn-ghost btn-sm" disabled={busy}
                  title={paused ? "Start re-fetching these posts again" : "Stop re-fetching; posts already collected stay"}
                  onClick={() => act(() => api.streamSettings({ label: `wl:${w.watchlist_id}:0`, paused: !paused }))}>
            {paused ? "Resume" : "Pause"}
          </button>
          <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}
                  title={foreign ? "Unlink from this project — the list stays in the project that created it" : "Delete this watchlist"}>
            {foreign ? "Remove" : "Delete"}
          </button>
        </div>
      </div>
      <div className="dmeta">
        <span className="chip">{fmtN(summ.total ?? 0)} link{summ.total === 1 ? "" : "s"}</span>
        {summ.ok > 0 && <span className="chip good" title="last fetch returned the post">{fmtN(summ.ok)} ok</span>}
        {summ.pending > 0 && <span className="chip" title="not fetched yet, or retrying">{fmtN(summ.pending)} pending</span>}
        {summ.unavailable > 0 && <span className="chip warn" title="deleted, protected or suspended — last numbers kept">{fmtN(summ.unavailable)} unavailable</span>}
        <SharedLine w={w} pid={pid} />
      </div>
      {renaming && <RenameModal w={w} onChanged={onChanged} onClose={() => setRenaming(false)} />}

      <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.5 }}>
        {sheet ? (
          <>
            {w.sheet_day && <><b>Day {fmtDay(w.sheet_day)}</b> · </>}
            Mirrors tab <b>“{w.sheet_tab || w.name}”</b> of sheet <b>“{sheet.title || sheet.sheet_id}”</b>
            {" · "}synced {sheet.last_sync_ms ? fmtAgo(sheet.last_sync_ms) : "never"}
            {" · "}re-read every {Math.round((sheet.sync_every_s || 600) / 60)} min
            {" · "}<a href={`https://docs.google.com/spreadsheets/d/${sheet.sheet_id}`} target="_blank" rel="noreferrer">open sheet ↗</a>
          </>
        ) : (
          <>Pasted list — add more links below. Every post is re-fetched on the cadence above; the numbers you see are the latest read.</>
        )}
        {w.last_refresh_ms ? <> · last pass {fmtAgo(w.last_refresh_ms)}</> : null}
      </div>
      {sheet?.last_error && (
        <div className="banner-warn" style={{ marginTop: 10 }}>
          <b>Sheet not read:</b> {sheet.last_error}
        </div>
      )}

      <div className="filters" style={{ margin: "12px 0 0" }}>
        <input value={search} placeholder={`search ${data.length} post${data.length === 1 ? "" : "s"}…`}
               style={{ flex: 1, minWidth: 160 }} onChange={(e) => setSearch(e.target.value)} />
        <select value={sort} onChange={(e) => setSort(e.target.value)} title="sort">
          {LINK_SORTS.map(([v, t]) => <option key={v} value={v}>sort: {t}</option>)}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} title="state">
          <option value="">all states</option>
          <option value="ok">ok</option>
          <option value="pending">pending</option>
          <option value="unavailable">unavailable</option>
          <option value="removed">removed</option>
        </select>
        <select value={groupBy} onChange={(e) => setGroupBy(e.target.value)} title="group rows">
          <option value="">no grouping</option>
          <option value="author">group by author</option>
          {hasSections && <option value="section">group by section</option>}
        </select>
      </div>

      <div className="members-box" style={{ maxHeight: 520, padding: 0, margin: "8px 0", overflow: "auto" }}>
        {rows.loading && !rows.data && <Loading />}
        {rows.error && <ErrorState error={rows.error} retry={rows.reload} />}
        {rows.data && data.length === 0 && (
          <div style={{ color: "var(--ink-3)", fontSize: 13, padding: 14 }}>
            {status ? `No ${status} links.` : sheet
              ? "No x.com post links found in this tab yet — paste some into the sheet, or below."
              : "No links yet — paste some below."}
          </div>
        )}
        {rows.data && data.length > 0 && shown.length === 0 && (
          <div style={{ color: "var(--ink-3)", fontSize: 13, padding: 14 }}>no match for “{search}”</div>
        )}
        {shown.length > 0 && !groupBy && (
          <table className="tbl">
            <thead>{head}</thead>
            <tbody>
              {shown.map((r) => (
                <LinkRow key={r.tweet_id} row={r} busy={busy}
                         onRemove={(row) => act(() => api.removeLink(w.watchlist_id, row.tweet_id))} />
              ))}
            </tbody>
          </table>
        )}
        {shown.length > 0 && groupBy && groups.map((g) => (
          <div key={g.key} style={{ borderBottom: "1px solid var(--ring)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 9, padding: "9px 10px 4px", fontSize: 13 }}>
              {groupBy === "author" && (g.avatar
                ? <img src={g.avatar} alt="" style={{ width: 22, height: 22, borderRadius: "50%" }} />
                : <span className="pfp" style={{ width: 22, height: 22, fontSize: 10, background: "var(--brand)" }}>{g.key.slice(1, 2).toUpperCase()}</span>)}
              <b>{g.key}</b>
              {g.followers != null && <span style={{ color: "var(--ink-3)", fontSize: 12 }}>{fmtN(g.followers)} followers</span>}
              <span className="chip" style={{ marginLeft: "auto" }}>{g.rows.length} post{g.rows.length === 1 ? "" : "s"}</span>
              <span className="chip" title="views summed over these posts">👁 {fmtN(g.views)}</span>
              <span className="chip" title="likes summed over these posts">❤ {fmtN(g.likes)}</span>
            </div>
            <table className="tbl">
              <tbody>
                {g.rows.map((r) => (
                  <LinkRow key={r.tweet_id} row={r} busy={busy} showSection={groupBy !== "section"}
                           onRemove={(row) => act(() => api.removeLink(w.watchlist_id, row.tweet_id))} />
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>

      <div className="filters" style={{ marginBottom: 0, alignItems: "flex-start" }}>
        <textarea rows="2" value={adding} style={{ flex: 1, minWidth: 220, font: "inherit" }}
                  placeholder={"paste x.com post links — one per line, or several separated by spaces"}
                  onChange={(e) => setAdding(e.target.value)} />
        <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()}
                onClick={() => act(() => api.addLinks(w.watchlist_id, adding),
                                   (r) => { setAdding(""); return `${r.added} added${r.revived ? `, ${r.revived} revived` : ""}${r.existing ? `, ${r.existing} already listed` : ""}${r.skipped ? `, ${r.skipped} skipped (not X post links)` : ""}${r.note ? ` — ${r.note}` : ""}`; })}>
          Add
        </button>
      </div>
      {msg && <div style={{ color: "var(--good-text)", fontSize: 13, marginTop: 8 }}>{msg}</div>}
      {err && <div style={{ color: "var(--critical)", fontSize: 13, marginTop: 8 }}>{err}</div>}

      <details className="help">
        <summary>How a links watchlist works</summary>
        <p>
          Each post is fetched by id once per cycle and its counters are
          overwritten with the latest numbers; <b>views</b> is the reach figure.
          Nothing is kept as history here — the tool reading <code>/api/links</code>
          keeps its own. A deleted or protected post is marked <i>unavailable</i>
          and keeps its last numbers. A link that leaves the sheet, or that you
          remove, is marked and never fetched again, but its post stays.
          {sheet ? " New rows in the sheet start within a couple of minutes of the next sync." : ""}
        </p>
      </details>

      {confirming && (
        <WatchlistDeleteModal w={w} pid={pid} onClose={() => setConfirming(false)} onChanged={onChanged}
                              sub="Refreshing stops for these links. The posts already collected stay in the database." />
      )}
    </div>
  );
}

// Facebook detail panel — pages + fetch. Configuration lives in the

function FbDetail({ pid, data, reload, gotoSettings, onBack }) {
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [fetching, setFetching] = useState(false);
  const [result, setResult] = useState(null);
  const sources = sortBy(data?.sources || [], "label");
  const paused = !!data?.paused;
  const health = data?.health || {};
  const nm = useApi(() => api.identities("fb"), []);
  const nameOf = (h) => (nm.data?.names || {})[String(h).toLowerCase()] || "";
  const editName = async (handle) => {
    const val = prompt(
      "Common display name for this page — links X / FB / IG by name and fixes the "
      + "profile picture (use the person's real name, same across platforms):",
      nameOf(handle));
    if (val === null) return;
    try { await api.setIdentity("fb", handle, val); nm.reload(); }
    catch (e) { alert(String(e.message || e)); }
  };

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
    <div className="panel detail">
      <div className="dhead">
        <div className="dtitle">
          {onBack && <button className="icon-btn back" onClick={onBack} aria-label="All watchlists">{icons.back}</button>}
          <span className="badge platform-fb">f</span>
          <h3>Facebook pages</h3>
          <span className={`chip ${paused ? "warn" : "good"}`}>{paused ? "paused" : "collecting"}</span>
          {(health.blocked || !data?.enabled) && (
            <button className="chip crit as-btn" onClick={gotoSettings} title="Open Network & settings to fix the login">
              {health.blocked ? "login needs a human" : "login not set up"} <span className="chev">›</span>
            </button>
          )}
        </div>
        <div className="dactions">
          <button className="btn btn-brand btn-sm" disabled={fetching || paused || sources.length === 0} onClick={run(api.fbFetch)}
                  title="Visit every page now instead of waiting for its cadence">
            {fetching ? "Fetching…" : "Fetch now"}
          </button>
          <button className="btn btn-ghost btn-sm" disabled={fetching || paused} onClick={run(api.fbFavorites, true)}
                  title="Read the collector account's Favourites feed once — richer data, one pass. Needs no pages here.">
            {fetching ? "…" : "Favorites feed"}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={gotoSettings}>Settings →</button>
        </div>
      </div>
      <div className="dmeta">
        <span className="chip">{sources.length} page{sources.length === 1 ? "" : "s"}</span>
        <span className="chip">{fmtN(data?.totals?.posts ?? 0)} collected</span>
        {data?.config?.mode === "favorites" && <span className="chip" title="Posts come from the collector account's Favourites feed">favorites mode</span>}
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
              <PillSelect label="every" value={s.speed || ""} className="sm"
                          options={[["", "default"], ...Object.entries(FB_SPEEDS)]}
                          title="How often this page is checked. 'default' follows the page cadence in Network & settings."
                          onChange={async (v) => { await api.fbSetInterval(s.label, v); reload(); }} />
              <button className="btn btn-ghost btn-sm"
                      onClick={async () => { await api.fbSetEnabled(s.label, !s.enabled); reload(); }}>
                {s.enabled ? "Pause" : "Resume"}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => editName(s.label)}
                      title="Set a common name to link this across X/FB/IG and fix the picture">
                Name{nameOf(s.label) ? " ✓" : ""}
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

// "Waiting for a profile id" — the manual way past a throttled name lookup.
export function IgIdPending({ pid, sources, reload }) {
  const [open, setOpen] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [draft, setDraft] = useState({});
  const [paste, setPaste] = useState("");
  const [busy, setBusy] = useState("");
  const [report, setReport] = useState(null);
  const [err, setErr] = useState("");

  // Only `user` sources carry an id. A hashtag or the following feed has no
  // profile to resolve, so listing them here would be a permanent false alarm.
  const pending = useMemo(
    () => sources.filter((s) => (s.type || "user") === "user" && !s.platform_id),
    [sources]);

  // Handle -> label, over EVERY source (not just the pending ones): an
  const byHandle = useMemo(() => {
    const m = new Map();
    for (const s of sources) {
      for (const k of [s.value, s.label]) {
        const h = cleanHandle(k);
        if (h) m.set(h.toLowerCase(), s.label);
      }
    }
    return m;
  }, [sources]);

  if (pending.length === 0) return null;

  const handleOf = (s) => cleanHandle(s.value || s.label) || s.label;

  const copy = async (text, what) => {
    setErr("");
    try { await navigator.clipboard.writeText(text); setReport({ note: `${what} copied` }); }
    catch { window.prompt(`Copy ${what}:`, text); }
  };

  const saveOne = async (label, raw) => {
    const id = cleanId(raw);
    if (!id) { setErr(`${label}: a profile id is digits only`); return; }
    setErr(""); setBusy(label); setReport(null);
    try {
      await api.igSource({ project: pid, action: "set-id", label, platform_id: id });
      setDraft((d) => { const n = { ...d }; delete n[label]; return n; });
      reload();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(""); }
  };

  const applyPaste = async () => {
    const { pairs, bad } = parseIgIds(paste);
    // Last line wins, so a corrected id further down the paste is the one
    // that lands rather than whichever the loop happened to write last.
    const wanted = new Map();
    for (const p of pairs) wanted.set(p.handle.toLowerCase(), p.id);

    const hits = [], unknown = [];
    for (const [handle, id] of wanted) {
      const label = byHandle.get(handle);
      if (label) hits.push({ label, handle, id }); else unknown.push(handle);
    }
    if (hits.length === 0) {
      setReport({ saved: 0, unknown, bad, failed: [] });
      return;
    }
    setErr(""); setBusy("paste"); setReport(null);
    const failed = [];
    for (const h of hits) {
      try {
        await api.igSource({ project: pid, action: "set-id",
                             label: h.label, platform_id: h.id });
      } catch (e) { failed.push(`${h.handle} (${String(e.message || e)})`); }
    }
    setBusy("");
    setPaste("");
    setReport({ saved: hits.length - failed.length, unknown, bad, failed });
    reload();
  };

  const parsed = paste.trim() ? parseIgIds(paste) : null;

  return (
    <div className="idp">
      <button className="idp-bar" onClick={() => setOpen(!open)}
              aria-expanded={open}
              title="These handles have no numeric Instagram id, so nothing can be collected for them">
        <span className="chip warn">{pending.length} waiting for a profile id</span>
        <span className="idp-hint">
          no id means no posts — paste them in to unblock collection
        </span>
        <span className="idp-caret">{open ? "▴" : "▾"}</span>
      </button>

      {open && (
        <div className="idp-body">
          <div className="idp-tools">
            <button className="btn btn-ghost btn-sm"
                    onClick={() => copy(pending.map(handleOf).join("\n"), "handles")}>
              Copy handles
            </button>
            <button className="btn btn-ghost btn-sm"
                    onClick={() => copy(
                      "{\n" + pending.map((s) => `  "${handleOf(s)}": ""`).join(",\n") + "\n}",
                      "JSON template")}>
              Copy JSON template
            </button>
            <span className="grow" />
            <button className="btn btn-ghost btn-sm" onClick={() => setPasteOpen(!pasteOpen)}>
              {pasteOpen ? "Close paste box" : "Paste a list →"}
            </button>
          </div>

          {pasteOpen && (
            <div className="idp-paste">
              <textarea rows={4} value={paste} spellCheck={false}
                        onChange={(e) => setPaste(e.target.value)}
                        placeholder={'natgeo 787132       (also: natgeo,787132 · natgeo: 787132)\n'
                          + 'nasa 528817151\n\n'
                          + 'or JSON: {"natgeo": "787132", "nasa": "528817151"}'} />
              <div className="idp-tools">
                <span className="idp-hint">
                  {parsed
                    ? `${parsed.pairs.length} id${parsed.pairs.length === 1 ? "" : "s"} read`
                      + (parsed.bad.length ? ` · ${parsed.bad.length} line${parsed.bad.length === 1 ? "" : "s"} unreadable` : "")
                    : "Handles are matched to this watchlist — nothing new is created."}
                </span>
                <span className="grow" />
                <button className="btn btn-brand btn-sm"
                        disabled={busy === "paste" || !parsed || parsed.pairs.length === 0}
                        onClick={applyPaste}>
                  {busy === "paste" ? "Saving…" : "Apply ids"}
                </button>
              </div>
            </div>
          )}

          {report && (
            <div className="idp-report">
              {report.note && <span>{report.note}</span>}
              {report.saved > 0 && (
                <b style={{ color: "var(--brand)" }}>
                  {report.saved} id{report.saved === 1 ? "" : "s"} saved
                </b>
              )}
              {report.saved === 0 && !report.note && <b>Nothing matched</b>}
              {report.unknown?.length > 0 && (
                <span> · not in this watchlist: {report.unknown.join(", ")}</span>
              )}
              {report.bad?.length > 0 && (
                <span> · unreadable: {report.bad.slice(0, 5).map((b) => `“${b}”`).join(", ")}
                  {report.bad.length > 5 ? ` +${report.bad.length - 5} more` : ""}</span>
              )}
              {report.failed?.length > 0 && (
                <span className="st-crit"> · rejected: {report.failed.join(", ")}</span>
              )}
            </div>
          )}

          <div className="idp-list">
            {pending.map((s) => {
              const h = handleOf(s);
              return (
                <div className="idp-row" key={s.label}>
                  <a className="idp-handle" href={`https://www.instagram.com/${h}/`}
                     target="_blank" rel="noreferrer"
                     title="Open the profile — the id is in the page source next to “profile_id”">
                    {h}
                  </a>
                  <input value={draft[s.label] ?? ""} inputMode="numeric" spellCheck={false}
                         placeholder="profile id"
                         onChange={(e) => setDraft((d) => ({ ...d, [s.label]: e.target.value }))}
                         onKeyDown={(e) => {
                           if (e.key === "Enter" && (draft[s.label] || "").trim()) {
                             saveOne(s.label, draft[s.label]);
                           }
                         }} />
                  <button className="btn btn-brand btn-sm"
                          disabled={busy === s.label || !cleanId(draft[s.label])}
                          onClick={() => saveOne(s.label, draft[s.label])}>
                    {busy === s.label ? "…" : "Save"}
                  </button>
                </div>
              );
            })}
          </div>
          {err && <div className="idp-err">{err}</div>}
        </div>
      )}
    </div>
  );
}

// Instagram detail panel

function IgDetail({ pid, data, reload, gotoSettings, onBack }) {
  const [msg, setMsg] = useState("");
  const [fetching, setFetching] = useState(false);
  const [result, setResult] = useState(null);
  const [adding, setAdding] = useState("");
  const [busyAdd, setBusyAdd] = useState(false);
  const sources = sortBy(data?.sources || [], "label");
  const paused = !!data?.paused;
  const anyCheckpoint = (data?.accounts || []).some((a) => a.checkpoint_at);
  const anyActive = (data?.accounts || []).some((a) => a.active);
  const nm = useApi(() => api.identities("ig"), []);
  const nameOf = (h) => (nm.data?.names || {})[String(h).toLowerCase()] || "";
  const editName = async (handle) => {
    const val = prompt(
      "Common display name for this account — links X / FB / IG by name and fixes the "
      + "profile picture (use the person's real name, same across platforms):",
      nameOf(handle));
    if (val === null) return;
    try { await api.setIdentity("ig", handle, val); nm.reload(); }
    catch (e) { alert(String(e.message || e)); }
  };
  const act = async (body) => {
    setMsg("");
    try { await api.igSource({ project: pid, ...body }); reload(); }
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

  // Facebook-style inline add: paste one or many usernames, added as user
  // sources (label = username; the engine resolves it to the pk at collect time).
  const addBulk = async () => {
    setBusyAdd(true); setMsg("");
    try {
      for (const u of adding.split(/[\s,]+/).filter(Boolean)) {
        const name = u.replace(/^[@#]/, "");
        await api.igSource({ action: "add", label: name, type: "user", value: name, project: pid });
      }
      setAdding(""); reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setBusyAdd(false); }
  };

  return (
    <div className="panel detail">
      <div className="dhead">
        <div className="dtitle">
          {onBack && <button className="icon-btn back" onClick={onBack} aria-label="All watchlists">{icons.back}</button>}
          <span className="badge platform-ig">IG</span>
          <h3>Instagram sources</h3>
          <span className={`chip ${paused ? "warn" : anyActive ? "good" : "warn"}`}>
            {paused ? "paused" : anyActive ? "collecting" : "no active session"}
          </span>
          {(anyCheckpoint || !anyActive) && (
            <button className="chip crit as-btn" onClick={gotoSettings} title="Open Network & settings to fix the session">
              {anyCheckpoint ? "checkpoint — needs a human" : "not signed in"} <span className="chev">›</span>
            </button>
          )}
        </div>
        <div className="dactions">
          <button className="btn btn-brand btn-sm" disabled={fetching || paused || sources.length === 0} onClick={fetchNow}
                  title="Check every source now instead of waiting for the next cycle">
            {fetching ? "Fetching…" : "Fetch now"}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={gotoSettings}>Settings →</button>
        </div>
      </div>
      <div className="dmeta">
        <span className="chip">{sources.length} source{sources.length === 1 ? "" : "s"}</span>
        <span className="chip">{fmtN(data?.totals?.posts ?? 0)} collected</span>
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
      <IgIdPending pid={pid} sources={sources} reload={reload} />
      <div className="members-box" style={{ maxHeight: 320, padding: "0 12px" }}>
        {sources.map((s) => (
          <div className="wl-row" key={s.label} style={{ opacity: s.enabled ? 1 : 0.55 }}>
            <div className="who">
              <b>{s.label}{!s.enabled && " (paused)"}</b>
              <small>
                {s.type}{s.value ? ` · ${s.value}` : ""}
                {s.account ? ` · pinned to @${s.account}` : (s.collector ? ` · via @${s.collector}` : " · not yet assigned")}
                {s.platform_id ? "" : " · id pending"}
              </small>
            </div>
            <div className="right" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <button className="btn btn-ghost btn-sm" onClick={() => editName(s.value || s.label)}
                      title="Set a common name to link this across X/FB/IG and fix the picture">
                Name{nameOf(s.value || s.label) ? " ✓" : ""}
              </button>
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
            No Instagram sources yet — paste usernames below, or use “+ New watchlist”.
          </div>
        )}
      </div>
      <div className="filters" style={{ marginTop: 10, marginBottom: 0 }}>
        <input value={adding} placeholder="instagram usernames — one or many, e.g. natgeo nasa isro"
               style={{ flex: 1, minWidth: 200 }}
               onChange={(e) => setAdding(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && adding.trim() && addBulk()} />
        <button className="btn btn-brand btn-sm" disabled={busyAdd || !adding.trim()} onClick={addBulk}>
          Add
        </button>
      </div>
      {msg && <div style={{ color: "var(--critical)", fontSize: 12.5 }}>{msg}</div>}
    </div>
  );
}

// Network & settings tab — one card per network: is it running, is the login
// healthy, how often it checks, and the few switches that change that.

function StatusChips({ paused, login }) {
  return (
    <>
      <span className={`chip ${paused ? "warn" : "good"}`}>{paused ? "paused" : "collecting"}</span>
      {login && <span className={`chip ${login.cls}`}>{login.text}</span>}
    </>
  );
}

function NetHead({ platform, name, children, right }) {
  return (
    <div className="dhead">
      <div className="dtitle">
        <span className={`badge platform-${platform}`}>{{ x: "𝕏", fb: "f", ig: "IG" }[platform]}</span>
        <h3>{name}</h3>
        {children}
      </div>
      <div className="dactions">{right}</div>
    </div>
  );
}

// X — the watcher process, the lists that feed it, and how much it holds.
function XNetwork({ pid, xLists, status, guard }) {
  const s = status.data || {};
  const up = Boolean(s.watcher_pid);
  const paused = Boolean(s.collection_paused);
  const streams = xLists.flatMap((w) => w.streams || []);
  const live = streams.filter((x) => !x.paused).length;
  const collected = streams.reduce((a, x) => a + (x.tweets || 0), 0);
  const accounts = xLists.reduce((a, w) => a + (w.kind === "xlist" ? (w.xmembers?.count || 0) : w.kind === "query" ? (w.members || []).length : 0), 0);
  const g = guard.data || {};
  return (
    <div className="panel detail">
      <NetHead platform="x" name="X (Twitter)"
               right={<Link className="btn btn-ghost btn-sm" to="/accounts" title="Sign in, backups and failover live on Accounts & Sessions">Accounts →</Link>}>
        {!status.data ? null
          : !up ? <span className="chip crit" title="The watcher process is not running — start it with: python3 main.py watch --all">collector off</span>
          : <StatusChips paused={paused} />}
      </NetHead>
      <div className="kv-grid">
        <div className="kv"><span>Watchlists in this project<Hint text="Handle lists, keyword searches, X Lists and link sheets — all compiled into streams the collector polls." /></span><b>{fmtN(xLists.length)}</b></div>
        <div className="kv"><span>Live streams<Hint text="One stream per query the collector is actively checking. Paused watchlists do not count." /></span><b>{fmtN(live)} <small>of {fmtN(streams.length)}</small></b></div>
        <div className="kv"><span>Accounts followed<Hint text="Members across handle lists plus fetched X List members." /></span><b>{fmtN(accounts)}</b></div>
        <div className="kv"><span>Collected<Hint text="Posts stored through this project's X watchlists." /></span><b>{fmtN(collected)}</b></div>
        {guard.data && (
          <div className="kv"><span>Rate-limit guard<Hint text="The guard slows or blocks fetches when X's rate limits are close. Details on the Guard page." /></span>
            <b className={g.blocked ? "st-crit" : g.warnings?.length ? "st-warn" : "st-good"}>
              {g.blocked ? "blocked" : g.warnings?.length ? `${g.warnings.length} warning${g.warnings.length === 1 ? "" : "s"}` : "clear"}
            </b></div>
        )}
      </div>
      <div className="hint">Cadence, depth and filters are set per watchlist on the Watchlists tab. Sign-in and backup accounts are on Accounts &amp; Sessions.</div>
    </div>
  );
}

function FbHealthBanner({ health, onAction, busy }) {
  if (!health?.blocked) return null;
  return (
    <div className="banner-crit" style={{ margin: "10px 0 4px" }}>
      <b>Login needs a human</b> — automatic retries are stopped
      ({health.reason === "checkpoint" ? "verification checkpoint" : "login failed"}).
      <div style={{ marginTop: 4 }}>{health.detail}</div>
      <div className="hint">since {health.ts ? fmtAgo(health.ts * 1000) : "—"}{health.email ? ` · ${health.email}` : ""}</div>
      <div className="filters" style={{ marginTop: 8, marginBottom: 0 }}>
        <button className="btn btn-brand btn-sm" disabled={busy} onClick={() => onAction("clear")}>I fixed it — retry</button>
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onAction("reset_session")}
                title="Also deletes fb_state.json so the next run logs in completely fresh">Reset session</button>
      </div>
    </div>
  );
}

const FB_MODES = [["pages", "Pages"], ["favorites", "Favorites feed"]];

function FbSettings({ data, reload }) {
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const cfg = data?.config || {};
  const ses = data?.session || {};
  const paused = !!data?.paused;
  const f = form || {
    mode: cfg.mode || "pages",
    default_interval_s: String(cfg.default_interval_s || 21600),
    fav_interval_s: String(cfg.fav_interval_s || 3600),
  };
  const dirty = form && JSON.stringify(form) !== JSON.stringify({
    mode: cfg.mode || "pages", default_interval_s: String(cfg.default_interval_s || 21600), fav_interval_s: String(cfg.fav_interval_s || 3600),
  });

  const go = async (fn) => { setBusy(true); try { await fn(); reload(); } catch { /* toast */ } finally { setBusy(false); } };
  const login = !data?.enabled ? { cls: "crit", text: "login not set up" }
    : data?.health?.blocked ? { cls: "crit", text: "login blocked" } : { cls: "good", text: "login ok" };

  return (
    <div className="panel detail">
      <NetHead platform="fb" name="Facebook"
               right={<button className={`btn btn-sm ${paused ? "btn-brand" : "btn-ghost"}`} disabled={busy}
                              title="Master switch — the background service honours it within a minute"
                              onClick={() => go(() => api.fbControl(paused ? "resume" : "pause"))}>
                        {paused ? "Resume collection" : "Pause collection"}
                      </button>}>
        {data && <StatusChips paused={paused} login={login} />}
      </NetHead>

      {data && !data.enabled && (
        <div className="banner-crit" style={{ margin: "10px 0 4px" }}>
          <b>Not set up.</b> Add <code>FB_EMAIL / FB_PASSWORD</code> (or <code>FB_C_USER / FB_XS</code>) to <code>.env</code> on the server, then restart the dashboard.
        </div>
      )}
      <FbHealthBanner health={data?.health} onAction={(a) => go(() => api.fbHealthAction(a))} busy={busy} />

      <div className="kv-grid">
        <div className="kv"><span>Login account</span><b>{ses.identity || "not set"}{ses.method ? <small> · {ses.method}</small> : null}</b></div>
        <div className="kv"><span>Saved session<Hint text="fb_state.json on the server. With it, the collector reuses the login; without it, the next run logs in fresh." /></span>
          <b className={ses.state_saved ? "st-good" : "st-warn"}>{ses.state_saved ? "present" : "none — fresh login next run"}</b></div>
        <div className="kv"><span>Pages<Hint text="Pages this project follows — managed on the Watchlists tab under Facebook." /></span><b>{fmtN((data?.sources || []).length)}</b></div>
        <div className="kv"><span>Collected</span><b>{fmtN(data?.totals?.posts ?? 0)}</b></div>
        <div className="kv"><span>Bandwidth<Hint text="Facebook runs on the server's own connection with a monthly cap." /></span>
          <b>server IP{cfg.use_proxy ? " + proxy" : ""}{cfg.monthly_cap_gb ? <small> · cap {cfg.monthly_cap_gb} GB/mo</small> : null}</b></div>
      </div>

      <Sec label="Collection"
           hint="Pages mode visits each page on its own cadence, newest posts only. Favorites mode reads the collector account's Favourites feed (Facebook → Feeds → Favourites → Manage, up to 30 pages) as one richer pass with reaction counts."
           right={<button className="btn btn-brand btn-sm" disabled={busy || !dirty} onClick={() => go(() => api.fbSettings(f))}>Save</button>}>
        <div className="ctrl-row">
          <PillSelect label="mode" value={f.mode} options={FB_MODES} onChange={(v) => setForm({ ...f, mode: v })}
                      title="Pages: visit each page on its cadence. Favorites: one pass over the Favourites feed." />
          <PillSelect label="page cadence" value={f.default_interval_s}
                      options={INTERVAL_OPTS.filter(([v]) => Number(v) >= 3600)}
                      onChange={(v) => setForm({ ...f, default_interval_s: v })}
                      title="How often each page is checked unless the page sets its own speed." />
          <PillSelect label="favorites cadence" value={f.fav_interval_s} options={INTERVAL_OPTS}
                      onChange={(v) => setForm({ ...f, fav_interval_s: v })}
                      title="How often the Favourites feed is read (favorites mode only)." />
        </div>
        <div className="hint">Saved settings apply from the collector's next cycle — no restart.</div>
      </Sec>
    </div>
  );
}

const IG_INTERVALS = [
  ["120", "2 minutes"], ["300", "5 minutes"], ["600", "10 minutes"],
  ["1800", "30 minutes"], ["3600", "1 hour"],
];

function IgSettings({ data, reload }) {
  const [busy, setBusy] = useState(false);
  const accounts = sortBy(data?.accounts || [], "username");
  const paused = !!data?.paused;
  const checkpointed = accounts.filter((a) => a.checkpoint_at);
  const active = accounts.filter((a) => a.active);
  const interval = String(data?.config?.interval_s || 120);
  const go = async (fn) => { setBusy(true); try { await fn(); reload(); } catch { /* toast */ } finally { setBusy(false); } };
  const login = checkpointed.length ? { cls: "crit", text: "checkpoint — needs a human" }
    : active.length ? { cls: "good", text: `${active.length} session${active.length === 1 ? "" : "s"} active` } : { cls: "warn", text: "no active session" };

  return (
    <div className="panel detail">
      <NetHead platform="ig" name="Instagram"
               right={<>
                 <PillSelect label="check every" value={interval} options={IG_INTERVALS} disabled={busy}
                             title="How often every Instagram source is checked. Applies from the service's next cycle — no restart."
                             onChange={(v) => go(() => api.igSettings({ interval_s: v }))} />
                 <button className={`btn btn-sm ${paused ? "btn-brand" : "btn-ghost"}`} disabled={busy}
                         title="Master switch — the background service honours it within a minute"
                         onClick={() => go(() => api.igControl(paused ? "resume" : "pause"))}>
                   {paused ? "Resume collection" : "Pause collection"}
                 </button>
               </>}>
        {data && <StatusChips paused={paused} login={login} />}
      </NetHead>

      {checkpointed.map((a) => (
        <div className="banner-crit" key={a.username} style={{ margin: "10px 0 4px" }}>
          <b>@{a.username} is checkpoint-locked</b> — automatic relogins are stopped (since {a.checkpoint_at}).
          <div style={{ marginTop: 4 }}>
            On <Link to="/accounts">Accounts &amp; Sessions</Link>, press <b>Sign in → Open this account's browser</b> and complete the
            “confirm it's you” check there; or paste that browser's cookies into the same panel. Either clears this by itself.
          </div>
        </div>
      ))}

      <div className="kv-grid">
        <div className="kv"><span>Sources<Hint text="Instagram accounts this project follows — managed on the Watchlists tab under Instagram." /></span><b>{fmtN((data?.sources || []).length)}</b></div>
        <div className="kv"><span>Collected</span><b>{fmtN(data?.totals?.posts ?? 0)}</b></div>
        <div className="kv"><span>Accounts in rotation<Hint text="Server-side Instagram logins. Active ones collect in parallel and the collector balances sources between them." /></span>
          <b>{active.length} <small>of {accounts.length}</small></b></div>
      </div>

      <Sec label="Accounts" hint="Each active account owns a share of the sources. 'benched' means it is signed in but not collecting. Manage sign-ins on Accounts & Sessions.">
        {accounts.length === 0 && <div className="muted">No Instagram account onboarded yet — add one on <Link to="/accounts">Accounts &amp; Sessions</Link>.</div>}
        {accounts.map((a) => (
          <div className="kv" key={a.username}>
            <span>@{a.username}{a.identity ? <small className={a.identity.legacy ? "st-warn" : ""} title={a.identity.legacy ? "Legacy phone identity — sign in again to mint a fresh one" : "Phone identity this account presents"}> · {a.identity.name || a.identity.model}{a.identity.legacy ? " (legacy)" : ""}</small> : null}</span>
            <b className={a.active ? "st-good" : "st-warn"}>
              {a.active ? `collecting · ${a.owns ?? 0} source${a.owns === 1 ? "" : "s"}` : "benched"}{a.proxy ? " · proxied" : ""}
              {a.error ? <span className="st-crit"> · {a.error}</span> : null}
            </b>
          </div>
        ))}
      </Sec>
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

// The view: tabs + master-detail

export default function Watchlists({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const wls = useApi(
    () => (pid ? api.watchlists(pid) : Promise.resolve({ watchlists: [] })),
    [pid],
  );
  const fb = useApi(() => api.fbStatus(pid), [pid], { every: 30_000 });
  const ig = useApi(() => api.igStatus(pid), [pid], { every: 60_000 });
  const status = useApi(() => api.status(), [], { every: 30_000 });
  const guard = useApi(() => api.guard(), [], { every: 120_000 });
  const [tab, setTab] = useState("lists");
  const [sel, setSel] = useState(null);        // "x:<id>" | "fb" | "ig" | null
  const [creating, setCreating] = useState(false);
  const [addingExisting, setAddingExisting] = useState(false);
  const mobile = useMediaQuery("(max-width: 1000px)");

  // /api/watchlists also carries one synthetic kind:"instagram" row per project (the ig:P:0 stream,
  // for Watch Tower).
  const xLists = useMemo(() => sortBy((wls.data?.watchlists || []).filter(
    (w) => w.platform !== "instagram" && w.kind !== "instagram",
  )), [wls.data]);
  const items = useMemo(() => {
    const out = xLists.map((w) => ({
      id: `x:${w.watchlist_id}`, platform: "x", name: w.name,
      sub: (w.kind === "xlist"
        ? `X List · ${w.xmembers?.count ? `${w.xmembers.count} accounts` : "members not fetched"}`
          + `${w.owner_handle ? ` · @${w.owner_handle}` : ""}`
        : w.kind === "links"
        ? `${w.links?.total ?? 0} links · every ${Math.round((w.refresh_every_s || 86400) / 3600)}h`
          + (w.sheet ? (w.sheet_day ? ` · day ${fmtDay(w.sheet_day)}` : ` · sheet tab “${w.sheet_tab || w.name}”`) : "")
        : `${w.members.length} ${w.kind === "keywords" ? "keywords" : "handles"}`)
        + (w.owner_project_id && w.owner_project_id !== pid ? ` · from ${w.owner_project || "another project"}`
           : w.shared ? " · shared" : ""),
      live: w.streams.some((s) => !s.paused), w,
    }));
    if (out.length === 0) {
      out.push({
        id: "x", platform: "x", name: "X (Twitter) watchlists",
        sub: "none yet — handles, keywords, an X List, or post links", live: false,
      });
    }
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
  }, [xLists, fb.data, ig.data, pid]);

  // Desktop always shows a detail (the first list by default); a phone shows
  // the list until one is tapped, then the detail with a back button.
  const picked = items.find((i) => i.id === sel) || null;
  const selected = picked || (mobile ? null : items[0] || null);
  const showList = !mobile || !selected;
  const showDetail = !mobile || !!selected;
  const back = mobile ? () => setSel(null) : undefined;
  const reloadAll = () => { wls.reload(); fb.reload(); ig.reload(); };

  // How many X accounts this project follows, added up across its lists:
  const xTotals = useMemo(() => {
    let accounts = 0;
    const unfetched = [];
    for (const w of xLists) {
      if (w.kind === "xlist") {
        if (w.xmembers?.count) accounts += w.xmembers.count;
        else unfetched.push(w);
      } else if (w.kind === "query") {
        accounts += (w.members || []).length;
      }
    }
    return { accounts, unfetched };
  }, [xLists]);
  const [refreshingAll, setRefreshingAll] = useState("");
  const refreshUnfetched = async () => {
    for (const w of xTotals.unfetched) {
      setRefreshingAll(w.name);
      try { await api.refreshXlistMembers(w.list_id); } catch { /* shown per list */ }
    }
    setRefreshingAll("");
    wls.reload();
  };
  const [listFilter, setListFilter] = useState("");

  const groups = [["x", "X (Twitter)"], ["fb", "Facebook"], ["ig", "Instagram"]];
  const SCROLL_AT = 6, FILTER_AT = 8;

  return (
    <>
      <PageHead title="Watchlists" onMenu={onMenu}
                sub={project ? `${project.name} — who this project follows, on every platform` : ""}>
        <button className="btn btn-ghost" onClick={() => setAddingExisting(true)}
                title="Share a watchlist another project already has — one list, collected once, shown in both.">
          Add existing…
        </button>
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
            <div className={`wl-layout${mobile && showDetail ? " detail-only" : ""}`}>
              {showList && <div className="wl-list">
                {groups.map(([p, label]) => {
                  const all = items.filter((i) => i.platform === p);
                  if (all.length === 0) return null;
                  const filterable = p === "x" && xLists.length > FILTER_AT;
                  const q = filterable ? listFilter.trim().toLowerCase() : "";
                  const rows = q
                    ? all.filter((i) => `${i.name} ${i.sub}`.toLowerCase().includes(q))
                    : all;
                  const scroll = all.length > SCROLL_AT;
                  return (
                    <React.Fragment key={p}>
                      <div className="wl-group">
                        {label}
                        {p === "x" && xLists.length > 0 && <span className="cnt">· {xLists.length}</span>}
                        {p === "x" && xTotals.accounts > 0 && (
                          <span className="cnt"
                                title={"Accounts followed across this project's handle lists and X Lists. "
                                       + "Watch-Tower's “handles” is distinct authors seen in the posts "
                                       + "(retweets, collabs and past members included), so theirs runs higher."}>
                            · {fmtN(xTotals.accounts)} accounts
                          </span>
                        )}
                        {p === "x" && xTotals.unfetched.length > 0 && (
                          <button className="btn btn-ghost btn-sm" disabled={!!refreshingAll}
                                  title={"Members of these X Lists have never been pulled, so they are missing from the total: "
                                         + xTotals.unfetched.map((w) => w.name).join(", ")}
                                  onClick={refreshUnfetched}>
                            {refreshingAll ? `fetching ${refreshingAll}…`
                              : `${xTotals.unfetched.length} list${xTotals.unfetched.length === 1 ? "" : "s"} not counted — fetch`}
                          </button>
                        )}
                        {filterable && (
                          <input value={listFilter} placeholder={`filter ${xLists.length} watchlists…`}
                                 onChange={(e) => setListFilter(e.target.value)} />
                        )}
                      </div>
                      <div className={`wl-rows${scroll ? " scroll" : ""}`}>
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
                            <span className={`dot${i.live ? "" : " off"}`} title={i.live ? "collecting" : "not collecting"} />
                            {mobile && <span className="chev">›</span>}
                          </button>
                        ))}
                        {q && rows.length === 0 && (
                          <div className="muted" style={{ padding: "8px 12px" }}>no watchlist matches “{listFilter}”</div>
                        )}
                      </div>
                    </React.Fragment>
                  );
                })}
                {items.length === 0 && (
                  <div className="muted" style={{ padding: 14 }}>Nothing yet — “+ New watchlist”.</div>
                )}
              </div>}

              {showDetail && <div className="wl-detail" key={selected?.id || "none"}>
                {!selected && (
                  <Empty title="No watchlists in this project yet">
                    A watchlist is a set of accounts to collect. “+ New watchlist”
                    works for X, Facebook, and Instagram alike.
                  </Empty>
                )}
                {selected?.id === "x" && (
                  <div className="panel detail">
                    {back && <button className="btn btn-ghost btn-sm" onClick={back}>‹ All watchlists</button>}
                    <Empty title="No X watchlists in this project yet">
                      An X watchlist is a set of handles, a keyword search, an existing
                      X List, or a list of post links from a Google Sheet.
                      <div style={{ marginTop: 14 }}>
                        <button className="btn btn-brand" onClick={() => setCreating(true)}>
                          + New watchlist
                        </button>
                      </div>
                    </Empty>
                  </div>
                )}
                {selected?.platform === "x" && selected.w?.kind === "links" && (
                  <>{back && <button className="btn btn-ghost btn-sm back-btn" onClick={back}>‹ All watchlists</button>}
                  <LinksDetail pid={pid} w={selected.w} onChanged={reloadAll} /></>
                )}
                {selected?.platform === "x" && selected.w && selected.w.kind !== "links" && (
                  <XDetail w={selected.w} pid={pid} onChanged={reloadAll} onBack={back} />
                )}
                {selected?.id === "fb" && (
                  <FbDetail pid={pid} data={fb.data} reload={fb.reload} onBack={back}
                            gotoSettings={() => setTab("settings")} />
                )}
                {selected?.id === "ig" && (
                  <IgDetail pid={pid} data={ig.data} reload={ig.reload} onBack={back}
                            gotoSettings={() => setTab("settings")} />
                )}
              </div>}
            </div>
          )}
        </>
      )}

      {tab === "settings" && (
        <div className="net-grid">
          <XNetwork pid={pid} xLists={xLists} status={status} guard={guard} />
          <FbSettings data={fb.data} reload={fb.reload} />
          <IgSettings data={ig.data} reload={ig.reload} />
        </div>
      )}

      {creating && pid && (
        <AddModal pid={pid} onDone={reloadAll} onClose={() => setCreating(false)} />
      )}
      {addingExisting && pid && (
        <AddExistingModal pid={pid} onDone={reloadAll} onClose={() => setAddingExisting(false)} />
      )}
    </>
  );
}
