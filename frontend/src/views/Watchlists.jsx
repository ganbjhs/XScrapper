// Watchlist management: create (query- or X-List-backed), add/remove handles,
// see the compiled streams, delete. Everything talks to the Phase 1 endpoints.
import React, { useState } from "react";
import { api, fmtN, useApi } from "../api/client.js";
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
          <option value="xlist">Existing X List (fastest polling)</option>
        </select>
      </div>
      {kind === "xlist" ? (
        <div className="field">
          <label htmlFor="wlist">X List URL or id</label>
          <input id="wlist" value={listId} onChange={(e) => setListId(e.target.value)}
                 placeholder="https://x.com/i/lists/1234567890123456789" />
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

function WatchlistCard({ w, onChanged }) {
  const [adding, setAdding] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

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

  return (
    <div className="panel">
      <div className="phead">
        <h3>{w.name}</h3>
        <span className="right">
          {w.kind === "xlist"
            ? `X List ${w.list_id}`
            : `${w.members.length} handles → ${live.length} stream${live.length === 1 ? "" : "s"}`}
          {" · "}{fmtN(collected)} collected
        </span>
      </div>

      {w.kind === "query" && (
        <>
          <div style={{ margin: "8px 0 4px" }}>
            {w.members.map((mb) => (
              <span className="tag" key={mb.handle}>
                @{mb.handle}
                <button aria-label={`remove ${mb.handle}`} disabled={busy}
                        onClick={() => change([], [mb.handle])}>✕</button>
              </span>
            ))}
            {w.members.length === 0 && (
              <span style={{ color: "var(--ink-3)", fontSize: 13 }}>
                No handles yet — add some below.
              </span>
            )}
          </div>
          <div className="filters" style={{ marginTop: 10, marginBottom: 0 }}>
            <input value={adding} placeholder="@handle, profile URL, or several separated by spaces"
                   style={{ flex: 1, minWidth: 200 }}
                   onChange={(e) => setAdding(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && adding.trim() &&
                     change(adding.split(/[\s,]+/).filter(Boolean), [])} />
            <button className="btn btn-brand btn-sm" disabled={busy || !adding.trim()}
                    onClick={() => change(adding.split(/[\s,]+/).filter(Boolean), [])}>
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

      {creating && pid && (
        <CreateModal pid={pid} onDone={reload} onClose={() => setCreating(false)} />
      )}
    </>
  );
}
