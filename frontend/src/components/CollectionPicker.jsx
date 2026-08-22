// The "+ Collection" picker: pin one post into a board, or make the board
// right there. Small on purpose — pinning must cost one click, not a form.
import React, { useState } from "react";
import { api, useApi } from "../api/client.js";
import { Modal } from "./ui.jsx";

export default function CollectionPicker({ t, pid, onClose }) {
  const { data, reload } = useApi(() => api.collections(pid), [pid]);
  const [newName, setNewName] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState("");

  const pin = async (cid, name) => {
    setErr("");
    try {
      // Pins carry their platform now — a board can hold X, Instagram and
      // Facebook posts, and an id alone no longer says which.
      await api.collectionPin(
        cid, [{ platform: t.platform || "x", post_id: String(t.tweet_id) }], []);
      setDone(name);
      setTimeout(onClose, 650);
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const createAndPin = async () => {
    setErr("");
    try {
      const made = await api.createCollection(pid, newName);
      await pin(made.collection_id, made.name);
      reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <Modal title="Add to collection" sub={`@${t.author_username} — “${(t.text || "").slice(0, 60)}…”`}
           onClose={onClose}>
      {done ? (
        <div style={{ padding: "14px 0", fontWeight: 600 }} className="st-good">
          ✓ Pinned to “{done}”
        </div>
      ) : (
        <>
          <div style={{ margin: "12px 0 4px" }}>
            {(data?.collections || []).map((c) => (
              <button key={c.collection_id} className="btn btn-ghost btn-sm"
                      style={{ margin: "0 6px 8px 0" }}
                      onClick={() => pin(c.collection_id, c.name)}>
                {c.name} <span style={{ color: "var(--ink-3)" }}>({c.items})</span>
              </button>
            ))}
            {data && data.collections.length === 0 && (
              <div style={{ color: "var(--ink-3)", fontSize: 13, marginBottom: 8 }}>
                No collections in this project yet — name the first one below.
              </div>
            )}
          </div>
          <div className="filters" style={{ marginBottom: 0 }}>
            <input placeholder="New collection, e.g. Floods — day 2" value={newName}
                   style={{ flex: 1 }} onChange={(e) => setNewName(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && newName.trim() && createAndPin()} />
            <button className="btn btn-brand btn-sm" disabled={!newName.trim()}
                    onClick={createAndPin}>
              Create & pin
            </button>
          </div>
          {err && <div className="err" style={{ color: "var(--critical)", fontSize: 13, marginTop: 10 }}>{err}</div>}
        </>
      )}
    </Modal>
  );
}
