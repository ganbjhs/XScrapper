// Curation boards: what an editor pinned, ready to hand off. Boards reference
// collected posts — deleting a board never touches the archive.
import React, { useState } from "react";
import { api, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import PostCard from "../components/PostCard.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

function Board({ c, onBack, onChanged }) {
  const { data, error, loading, reload } = useApi(
    () => api.collectionItems(c.collection_id), [c.collection_id]);

  const unpin = async (t) => {
    await api.collectionPin(c.collection_id, [], [String(t.tweet_id)]);
    reload();
    onChanged();
  };

  const exportUrl =
    `/api/collections/export?id=${c.collection_id}&name=${encodeURIComponent(c.name)}`;

  return (
    <>
      <div className="feed-head">
        <button className="btn btn-ghost btn-sm" onClick={onBack}>← All collections</button>
        <h2>{c.name}</h2>
        <span className="right">
          <a className="btn btn-brand btn-sm" href={exportUrl}>Download CSV</a>
        </span>
      </div>
      {loading && !data && <Loading />}
      {error && <ErrorState error={error} retry={reload} />}
      {data && data.rows.length === 0 && (
        <Empty title="Nothing pinned yet">
          Use “+ Collection” on any post in the Live Feed or Search.
        </Empty>
      )}
      {(data?.rows || []).map((t) => (
        <PostCard key={t.tweet_id} t={{ ...t, platform: "x" }} onUnpin={unpin} />
      ))}
    </>
  );
}

export default function Collections({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const { data, error, loading, reload } = useApi(
    () => (pid ? api.collections(pid) : Promise.resolve({ collections: [] })), [pid]);
  const [open, setOpen] = useState(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  const [confirming, setConfirming] = useState(null);

  const create = async () => {
    setErr("");
    try {
      await api.createCollection(pid, name);
      setCreating(false);
      setName("");
      reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  if (open) {
    return (
      <>
        <PageHead title="Collections" onMenu={onMenu} sub={project?.name} />
        <Board c={open} onBack={() => setOpen(null)} onChanged={reload} />
      </>
    );
  }

  return (
    <>
      <PageHead title="Collections" onMenu={onMenu}
                sub={project ? `${project.name} — pinned posts, ready to hand off` : ""}>
        <button className="btn btn-brand" onClick={() => setCreating(true)}>+ New collection</button>
      </PageHead>

      {loading && !data && <Loading />}
      {error && <ErrorState error={error} retry={reload} />}
      {data && data.collections.length === 0 && (
        <Empty title="No collections yet">
          A collection is a board you pin posts onto — “Floods day 2”, “CM
          statements” — then download as CSV or hand to the desk.
        </Empty>
      )}

      {(data?.collections || []).map((c) => (
        <div className="panel" key={c.collection_id}>
          <div className="phead">
            <h3>{c.name}</h3>
            <span className="right">{fmtN(c.items)} post{c.items === 1 ? "" : "s"}</span>
          </div>
          <div className="filters" style={{ marginBottom: 0, marginTop: 8 }}>
            <button className="btn btn-brand btn-sm" onClick={() => setOpen(c)}>Open</button>
            <a className="btn btn-ghost btn-sm"
               href={`/api/collections/export?id=${c.collection_id}&name=${encodeURIComponent(c.name)}`}>
              Download CSV
            </a>
            <span style={{ flex: 1 }} />
            <button className="btn btn-danger btn-sm" onClick={() => setConfirming(c)}>Delete</button>
          </div>
        </div>
      ))}

      {creating && (
        <Modal title="New collection" onClose={() => setCreating(false)}>
          <div className="field">
            <label htmlFor="cname">Name</label>
            <input id="cname" value={name} autoFocus onChange={(e) => setName(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && name.trim() && create()}
                   placeholder="e.g. Floods — day 2" />
          </div>
          {err && <div className="err">{err}</div>}
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setCreating(false)}>Cancel</button>
            <button className="btn btn-brand" disabled={!name.trim()} onClick={create}>Create</button>
          </div>
        </Modal>
      )}

      {confirming && (
        <Modal title={`Delete “${confirming.name}”?`} onClose={() => setConfirming(null)}
               sub="The board goes away. Every post stays in your archive.">
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setConfirming(null)}>Keep it</button>
            <button className="btn btn-danger"
                    onClick={async () => {
                      await api.removeCollection(confirming.collection_id);
                      setConfirming(null);
                      reload();
                    }}>
              Delete
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
