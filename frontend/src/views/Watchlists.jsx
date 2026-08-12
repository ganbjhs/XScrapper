// Watchlist management: create (query- or X-List-backed), add/remove handles,
// see the compiled streams, delete. Everything talks to the Phase 1 endpoints.
import React, { useState } from "react";
import { api, fmtAgo, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

function CreateModal({ pid, onDone, onClose }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState("query");
  const [listId, setListId] = useState("");
  const [handles, setHandles] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    setErr("");
    try {
      const body = { project: pid, name, kind };
      if (kind === "xlist") body.list_id = listId;
      else if (kind === "keywords")
        // One rule per LINE — a rule may contain spaces and AND.
        body.handles = handles.split(/\n+/).map((s) => s.trim()).filter(Boolean);
      else body.handles = handles.split(/[\s,]+/).filter(Boolean);
      const made = await api.createWatchlist(body);
      if (made.warning) setErr(made.warning);
      else {
        onDone();
        onClose();
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="New watchlist" onClose={onClose}
           sub="Handles compile into search streams the collector polls. An X List keeps the faster list rate-limit.">
      <div className="field">
        <label htmlFor="wname">Name</label>
        <input id="wname" value={name} autoFocus onChange={(e) => setName(e.target.value)}
               placeholder="e.g. Cabinet" />
      </div>
      <div className="field">
        <label htmlFor="wkind">Type</label>
        <select id="wkind" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="query">Handles (built here — no X List needed)</option>
          <option value="keywords">Keywords (topics, phrases, AND combinations)</option>
          <option value="xlist">Existing X List (fastest polling)</option>
        </select>
      </div>
      {kind === "xlist" ? (
        <div className="field">
          <label htmlFor="wlist">X List URL or id</label>
          <input id="wlist" value={listId} onChange={(e) => setListId(e.target.value)}
                 placeholder="https://x.com/i/lists/1234567890123456789" />
        </div>
      ) : kind === "keywords" ? (
        <div className="field">
          <label htmlFor="whandles">Keywords — one per line</label>
          <textarea id="whandles" rows="5" value={handles}
                    onChange={(e) => setHandles(e.target.value)}
                    placeholder={'finance AND gst\n"vishnu deo sai"\n#Chhattisgarh\nbudget -filter:retweets'} />
          <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
            Each line is one rule; lines combine as OR. <b>AND</b> between words
            means the post must contain both, in any order. Quotes = exact
            phrase. X search operators pass through.
          </div>
        </div>
      ) : (
        <div className="field">
          <label htmlFor="whandles">Handles — one per line, @ optional</label>
          <textarea id="whandles" rows="5" value={handles}
                    onChange={(e) => setHandles(e.target.value)}
                    placeholder={"@DrKirodilalBJP\nJoraramKumawat\nhttps://x.com/KirodiOffice"} />
        </div>
      )}
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !name.trim()} onClick={create}>
          Create watchlist
        </button>
      </div>
    </Modal>
  );
}

const splitAdd = (kind, raw) =>
  kind === "keywords"
    ? raw.split(/,|\n/).map((s) => s.trim()).filter(Boolean)
    : raw.split(/[\s,]+/).filter(Boolean);

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
    setBusy(true);
    setMsg("");
    try {
      await api.watchlistFilters(w.watchlist_id, f);
      setMsg("✓ Saved — collection uses the new filters from its next check");
      onChanged();
    } catch (e) {
      setMsg(`✗ ${String(e.message || e)}`);
    } finally {
      setBusy(false);
    }
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

function WatchlistCard({ w, onChanged }) {
  const [adding, setAdding] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(null);   // {old, val}

  const change = async (add, remove) => {
    setBusy(true);
    setErr("");
    try {
      await api.watchlistMembers(w.watchlist_id, add, remove);
      setAdding("");
      onChanged();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const live = w.streams.filter((s) => !s.paused);
  const collected = w.streams.reduce((a, s) => a + (s.tweets || 0), 0);
  const setInterval = async (seconds) => {
    await api.watchlistInterval(w.watchlist_id, seconds);
    onChanged();
  };
  // Current interval as a preset value ("" = default).
  const curInterval = w.interval_s ? String(w.interval_s) : "";

  return (
    <div className="panel">
      <div className="phead">
        <h3>{w.name}</h3>
        <span className="right">
          {w.kind === "xlist"
            ? `X List ${w.list_id}`
            : w.kind === "keywords"
              ? `${w.members.length} keywords → ${live.length} stream${live.length === 1 ? "" : "s"}`
              : `${w.members.length} handles → ${live.length} stream${live.length === 1 ? "" : "s"}`}
          {" · "}{fmtN(collected)} collected
        </span>
      </div>
      <div className="filters" style={{ marginBottom: 4, marginTop: 2 }}>
        <label className="fpill" style={{ padding: "7px 8px 7px 12px" }}>
          <span>Check every</span>
          <select value={curInterval} onChange={(e) => setInterval(e.target.value)}>
            <option value="">default (~5–15 min, auto)</option>
            <option value="300">5 minutes</option>
            <option value="600">10 minutes</option>
            <option value="900">15 minutes</option>
            <option value="1800">30 minutes</option>
            <option value="3600">1 hour</option>
          </select>
        </label>
        <span style={{ color: "var(--ink-3)", fontSize: 12, alignSelf: "center" }}>
          how often the collector re-checks this watchlist
        </span>
      </div>

      {w.kind !== "xlist" && (
        <>
          <div style={{ margin: "8px 0 4px" }}>
            {w.members.map((mb) => (
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
          </div>
          <div className="filters" style={{ marginTop: 10, marginBottom: 0 }}>
            <input value={adding}
                   placeholder={w.kind === "keywords"
                     ? 'finance AND gst  —  or several rules separated by commas'
                     : "@handle, profile URL, or several separated by spaces"}
                   style={{ flex: 1, minWidth: 200 }}
                   onChange={(e) => setAdding(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && adding.trim() &&
                     change(splitAdd(w.kind, adding), [])} />
            <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()}
                    onClick={() => change(splitAdd(w.kind, adding), [])}>
              Add
            </button>
            <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}>
              Delete watchlist
            </button>
          </div>
        </>
      )}
      {w.kind === "xlist" && (
        <div className="filters" style={{ marginTop: 8, marginBottom: 0 }}>
          <span style={{ color: "var(--ink-3)", fontSize: 13 }}>
            Collected through the X List — members are managed on x.com.
          </span>
          <span style={{ flex: 1 }} />
          <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}>
            Delete watchlist
          </button>
        </div>
      )}
      {err && <div className="err" style={{ color: "var(--critical)", fontSize: 13, marginTop: 8 }}>{err}</div>}

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
                      setConfirming(false);
                      onChanged();
                    }}>
              Delete
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function StreamsManager({ pid }) {
  const { data, error, reload } = useApi(() => api.streamAssignments(), []);
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState("");
  if (error) return null;
  const streams = data?.streams || [];
  const mine = streams.filter((s) => s.projects.includes(pid));
  const others = streams.filter((s) => !s.projects.includes(pid));

  return (
    <div className="panel" style={{ marginTop: 22 }}>
      <div className="phead">
        <h3>Streams in this project</h3>
        <span className="right">
          what actually feeds this project's feed &amp; delivery ·{" "}
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Manage"}
          </button>
        </span>
      </div>
      {open && (
        <>
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
          {mine.length === 0 && (
            <div className="kv"><span>Nothing attached</span><b /></div>
          )}
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
            Removing a stream only changes what this project shows and
            delivers — the stream, its collection, and its posts stay.
          </div>
        </>
      )}
    </div>
  );
}

// Must match FB_SPEEDS in web.py — the named cadences a page can be checked at.
const FB_SPEEDS = { "1h": "1 hour", "3h": "3 hours", "6h": "6 hours",
                    "12h": "12 hours", "24h": "24 hours" };

function FacebookSources({ pid }) {
  const { data, error, reload } = useApi(() => api.fbStatus(pid), [pid]);
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [fetching, setFetching] = useState(false);
  const [result, setResult] = useState(null);
  if (error) return null;
  const sources = data?.sources || [];

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

  const fetchNow = async () => {
    setFetching(true); setResult(null); setMsg("");
    try {
      const r = await api.fbFetch(pid);
      if (r.error) setMsg(r.error);
      else setResult(r);
      reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setFetching(false); }
  };

  const fetchFavorites = async () => {
    setFetching(true); setResult(null); setMsg("");
    try {
      const r = await api.fbFavorites(pid);
      if (r.error) setMsg(r.error);
      else setResult({ ...r, favorites: true });
      reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setFetching(false); }
  };

  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="phead">
        <h3><span className="badge platform-fb" style={{ marginRight: 8 }}>f</span>Facebook pages</h3>
        <span className="right" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span>{data?.totals?.posts ?? 0} collected</span>
          <button className="btn btn-ghost btn-sm" disabled={fetching || sources.length === 0}
                  onClick={fetchFavorites}
                  title="Read the account's Favorites feed once and attribute posts to your pages — richer data, one pass">
            {fetching ? "…" : "Fetch Favorites feed"}
          </button>
          <button className="btn btn-brand btn-sm" disabled={fetching || sources.length === 0}
                  onClick={fetchNow}>
            {fetching ? "Fetching…" : "Fetch now"}
          </button>
        </span>
      </div>
      {!data?.enabled && (
        <div className="kv"><span>Not set up</span>
          <b className="st-warn">add FB_EMAIL / FB_PASSWORD (or FB_C_USER / FB_XS) to .env on the server</b></div>
      )}
      {fetching && (
        <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "6px 0" }}>
          Opening Facebook on the server and reading newest posts — this takes up to a minute.
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
            <pre style={{ whiteSpace: "pre-wrap", background: "var(--surface-2)",
                          padding: "8px 10px", borderRadius: 8, marginTop: 6,
                          fontSize: 11.5, color: "var(--ink-3)", maxHeight: 160,
                          overflow: "auto" }}>
              {result.log.join("\n")}
            </pre>
          )}
        </div>
      )}
      <div style={{ margin: "8px 0 4px" }}>
        {sources.map((s) => (
          <div className="wl-row" key={s.label}
               style={{ opacity: s.enabled ? 1 : 0.55 }}>
            <div className="who">
              <b>{s.label}{!s.enabled && " (paused)"}</b>
              <small>
                {fmtN(s.posts)} collected ·{" "}
                {s.last_run ? `checked ${fmtAgo(s.last_run * 1000)}` : "not checked yet"}
              </small>
            </div>
            <div className="right" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <label className="fpill" title="how often this page is checked">
                <span>every</span>
                <select value={s.speed || ""}
                        onChange={async (e) => { await api.fbSetInterval(s.label, e.target.value); reload(); }}>
                  <option value="">default (6h)</option>
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
                      onClick={async () => { await api.fbRemoveSource(s.label); reload(); }}>Remove</button>
            </div>
          </div>
        ))}
        {sources.length === 0 && (
          <span style={{ color: "var(--ink-3)", fontSize: 13 }}>
            No Facebook pages yet — add a page's handle (from its URL, e.g. “narendramodi”).
          </span>
        )}
      </div>
      <div className="filters" style={{ marginTop: 10, marginBottom: 0 }}>
        <input value={adding} placeholder="facebook page handle, e.g. narendramodi"
               style={{ flex: 1, minWidth: 200 }}
               onChange={(e) => setAdding(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && adding.trim() && add()} />
        <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()} onClick={add}>Add page</button>
      </div>
      {msg && <div style={{ color: "var(--critical)", fontSize: 12.5, marginTop: 8 }}>{msg}</div>}
      <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 8 }}>
        Facebook runs on the server’s own bandwidth with a monthly cap — it checks each page a few
        times a day, newest posts only.
      </div>
      <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
        <b>Favorites feed (richer):</b> in the collector’s Facebook account, add these pages to
        <b> Favorites</b> (Facebook → Feeds → Favorites → Manage, up to 30). Then “Fetch Favorites
        feed” reads them all as one real feed — which returns profile pictures, reaction counts and
        more posts than a page-by-page check. Posts are matched back to the pages above.
      </div>
    </div>
  );
}

export default function Watchlists({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const { data, error, loading, reload } = useApi(
    () => (pid ? api.watchlists(pid) : Promise.resolve({ watchlists: [] })),
    [pid],
  );
  const [creating, setCreating] = useState(false);

  return (
    <>
      <PageHead title="Watchlists" onMenu={onMenu}
                sub={project ? `${project.name} — who this project follows` : ""}>
        <button className="btn btn-brand" onClick={() => setCreating(true)}>+ New watchlist</button>
      </PageHead>

      {loading && !data && <Loading />}
      {error && <ErrorState error={error} retry={reload} />}
      {data && data.watchlists.length === 0 && (
        <Empty title="No watchlists in this project yet">
          A watchlist is a set of accounts to collect. Paste handles — the tool
          builds the search streams itself.
        </Empty>
      )}
      {(data?.watchlists || []).map((w) => (
        <WatchlistCard key={w.watchlist_id} w={w} onChanged={reload} />
      ))}

      {pid && <FacebookSources pid={pid} />}
      {pid && <StreamsManager pid={pid} />}

      {creating && pid && (
        <CreateModal pid={pid} onDone={reload} onClose={() => setCreating(false)} />
      )}
    </>
  );
}
