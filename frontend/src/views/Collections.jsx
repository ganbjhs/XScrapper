// Curation boards: what an editor pinned, ready to hand off — plus the boards
// the labeller fills on its own, one per category. Boards reference collected
// posts; deleting a board never touches the archive.
//
// Two tabs, the same shape the Watchlists page uses: "Boards" for daily work,
// "Labelling" for the vocabulary and the spend controls. New controls belong
// in the settings tab, never scattered across the main surface.
import React, { useState } from "react";
import { api, fmtAgo, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import PostCard from "../components/PostCard.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

const keyOf = (t) => `${t.platform || "x"}:${t.tweet_id}`;

function Board({ c, pid, cats, onBack, onChanged }) {
  const { data, error, loading, reload } = useApi(
    () => api.collectionItems(c.collection_id), [c.collection_id]);

  const unpin = async (t) => {
    await api.collectionPin(
      c.collection_id, [], [{ platform: t.platform || "x", post_id: String(t.tweet_id) }]);
    reload();
    onChanged();
  };

  const relabel = async (t, key) => {
    await api.setLabel(pid, t.platform || "x", String(t.tweet_id), key);
    // The post has just left this board for another one, so the board must
    // reload rather than keep showing a post it no longer holds.
    reload();
    onChanged();
  };

  const exportUrl =
    `/api/collections/export?id=${c.collection_id}&name=${encodeURIComponent(c.name)}`;
  const gone = (data?.pinned || 0) - (data?.count || 0);

  return (
    <>
      <div className="feed-head">
        <button className="btn btn-ghost btn-sm" onClick={onBack}>← All collections</button>
        <h2>{c.name}</h2>
        {c.auto ? <span className="badge cat">auto</span> : null}
        <span className="right">
          <a className="btn btn-brand btn-sm" href={exportUrl}>Download CSV</a>
        </span>
      </div>
      {c.auto ? (
        <div className="sub" style={{ margin: "0 0 10px" }}>
          Filled by the labeller — every post classified as “{c.name}” lands here.
          Change a post’s label and it moves to the matching board.
        </div>
      ) : null}
      {gone > 0 && (
        <div className="banner-warn" style={{ marginBottom: 10 }}>
          {gone} pinned post{gone === 1 ? " is" : "s are"} no longer in the
          archive — retention or a platform wipe removed{gone === 1 ? " it" : " them"}.
          The rest are below.
        </div>
      )}
      {loading && !data && <Loading />}
      {error && <ErrorState error={error} retry={reload} />}
      {data && data.rows.length === 0 && (
        <Empty title="Nothing pinned yet">
          {c.auto
            ? "No post carries this label yet. Run Classify from the Live Feed."
            : "Use “+ Collection” on any post in the Live Feed or Search."}
        </Empty>
      )}
      {(data?.rows || []).map((t) => (
        <PostCard key={keyOf(t)} t={t} onUnpin={unpin} cats={cats} onLabel={relabel} />
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// the labelling tab: the vocabulary the model is given, and the spend controls
// ---------------------------------------------------------------------------

function CategoryRow({ pid, cat, onSaved }) {
  const [c, setC] = useState(cat);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const dirty = c.name !== cat.name || c.description !== cat.description
    || String(c.rank) !== String(cat.rank);

  const save = async (patch = {}) => {
    setBusy(true); setMsg("");
    try {
      await api.saveLabelCategory(pid, {
        key: cat.key, name: c.name, description: c.description,
        rank: Number(c.rank), ...patch,
      });
      setMsg("✓ saved");
      onSaved();
    } catch (e) {
      setMsg(String(e.message || e));
    } finally {
      setBusy(false);
      setTimeout(() => setMsg(""), 4000);
    }
  };

  return (
    <div className="panel" style={{ opacity: cat.archived ? 0.6 : 1 }}>
      <div className="phead">
        <span className={`badge cat cat-${cat.key}`}>{cat.key}</span>
        <h3 style={{ margin: 0 }}>{cat.name}</h3>
        <span className="right" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
          precedence {cat.rank}{cat.archived ? " · archived" : ""}
        </span>
      </div>
      <div className="field">
        <label htmlFor={`n-${cat.key}`}>Name shown on the board and the chip</label>
        <input id={`n-${cat.key}`} value={c.name}
               onChange={(e) => setC({ ...c, name: e.target.value })} />
      </div>
      <div className="field">
        <label htmlFor={`d-${cat.key}`}>
          What the model is told — this text goes into the prompt verbatim
        </label>
        <textarea id={`d-${cat.key}`} rows={3} value={c.description}
                  onChange={(e) => setC({ ...c, description: e.target.value })} />
      </div>
      <div className="filters" style={{ marginBottom: 0 }}>
        <label className="fpill">
          <span>Precedence:</span>
          <input type="number" min="1" style={{ width: 70 }} value={c.rank}
                 onChange={(e) => setC({ ...c, rank: e.target.value })} />
        </label>
        <button className="btn btn-brand btn-sm" disabled={busy || !dirty}
                onClick={() => save()}>
          {busy ? "Saving…" : "Save"}
        </button>
        {cat.key !== "other" && (
          <button className="btn btn-ghost btn-sm" disabled={busy}
                  onClick={() => save({ archived: !cat.archived })}>
            {cat.archived ? "Bring back" : "Archive"}
          </button>
        )}
        <span style={{ flex: 1 }} />
        {msg && (
          <span className={msg.startsWith("✓") ? "st-good" : "st-crit"}
                style={{ fontSize: 12.5, fontWeight: 600 }}>{msg}</span>
        )}
      </div>
    </div>
  );
}

function Labelling({ pid }) {
  const st = useApi(() => (pid ? api.labelStatus(pid) : Promise.resolve(null)), [pid]);
  const cs = useApi(() => (pid ? api.labelCategories(pid) : Promise.resolve(null)), [pid]);
  const [adding, setAdding] = useState(false);
  const [nc, setNc] = useState({ key: "", name: "", description: "", rank: 50 });
  const [err, setErr] = useState("");
  const [set, setSet] = useState(null);
  const [setMsg, setSetMsg] = useState("");

  const d = st.data;
  React.useEffect(() => {
    if (d && set === null) {
      setSet({ model: d.model, cap_usd: d.cap_usd, max_posts: d.max_posts });
    }
  }, [d, set]);

  const addCat = async () => {
    setErr("");
    try {
      await api.saveLabelCategory(pid, { ...nc, rank: Number(nc.rank) });
      setAdding(false);
      setNc({ key: "", name: "", description: "", rank: 50 });
      cs.reload(); st.reload(true);
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const saveSettings = async () => {
    setSetMsg("");
    try {
      await api.saveLabelSettings(pid, set);
      setSetMsg("✓ saved — the next run uses these");
      st.reload(true);
    } catch (e) {
      setSetMsg(String(e.message || e));
    }
    setTimeout(() => setSetMsg(""), 5000);
  };

  if (!pid) return <Empty title="No project selected" />;
  if (st.loading && !d) return <Loading />;
  if (st.error) return <ErrorState error={st.error} retry={st.reload} />;

  const pct = d && d.cap_usd ? Math.min(100, (d.spent_usd / d.cap_usd) * 100) : 0;

  return (
    <>
      <div className="panel">
        <div className="phead"><h3>Where labelling stands</h3></div>
        {!d?.key_present && (
          <div className="banner-crit" style={{ margin: "8px 0" }}>
            No Grok key on the server. Add <code>XAI_API_KEY</code> to
            <code> .env</code> and restart the dashboard — the key is never
            stored or edited here.
          </div>
        )}
        <div className="cstats" style={{ fontSize: 13.5 }}>
          <span><b>{fmtN(d?.labelled || 0)}</b> labelled</span>
          <span><b>{fmtN(d?.unlabelled || 0)}</b> waiting</span>
          <span>model <b>{d?.model}</b></span>
          <span>
            ${Number(d?.spent_usd || 0).toFixed(2)} of ${Number(d?.cap_usd || 0).toFixed(2)}
            {" "}this month
          </span>
        </div>
        <div className="meter" style={{ marginTop: 8 }}>
          <span style={{ width: `${pct}%` }}
                className={pct >= 100 ? "crit" : pct >= 80 ? "warn" : ""} />
        </div>
        <div className="sub" style={{ marginTop: 8 }}>
          {d?.last_run
            ? `Last run ${fmtAgo(d.last_run.started_ms)} — ${d.last_run.labelled} labelled, `
              + `${d.last_run.failed} not, $${Number(d.last_run.cost_usd || 0).toFixed(4)}`
              + (d.last_run.stop_reason && d.last_run.stop_reason !== "done"
                ? ` (stopped: ${d.last_run.stop_reason})` : "")
            : "No run yet. Press Classify on the Live Feed."}
        </div>
      </div>

      {set && (
        <div className="panel">
          <div className="phead"><h3>Settings</h3></div>
          <div className="sub" style={{ marginBottom: 10 }}>
            Read fresh on every run, so nothing here needs a restart. The API
            key is the one exception and lives only in <code>.env</code>.
          </div>
          <div className="field">
            <label htmlFor="lmodel">Model</label>
            <input id="lmodel" value={set.model}
                   onChange={(e) => setSet({ ...set, model: e.target.value })} />
          </div>
          <div className="filters">
            <label className="fpill">
              <span>Monthly cap (USD):</span>
              <input type="number" min="1" step="1" style={{ width: 90 }}
                     value={set.cap_usd}
                     onChange={(e) => setSet({ ...set, cap_usd: e.target.value })} />
            </label>
            <label className="fpill">
              <span>Max posts per run:</span>
              <input type="number" min="1" step="25" style={{ width: 90 }}
                     value={set.max_posts}
                     onChange={(e) => setSet({ ...set, max_posts: e.target.value })} />
            </label>
            <button className="btn btn-brand btn-sm" onClick={saveSettings}>Save</button>
            <span style={{ flex: 1 }} />
            {setMsg && (
              <span className={setMsg.startsWith("✓") ? "st-good" : "st-crit"}
                    style={{ fontSize: 12.5, fontWeight: 600 }}>{setMsg}</span>
            )}
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            A run that would cross the cap is refused with the number named — it
            is never trimmed to fit. Roughly $0.25 per 1,000 posts at current
            prices.
          </div>
        </div>
      )}

      <div className="feed-head">
        <h2>Categories</h2>
        <span className="right">
          <button className="btn btn-brand btn-sm" onClick={() => setAdding(true)}>
            + New category
          </button>
        </span>
      </div>
      <div className="sub" style={{ margin: "0 0 10px" }}>
        Precedence decides ties: when a post could sit in two categories, the
        lower number wins, and the prompt says so.
      </div>
      {cs.loading && !cs.data && <Loading />}
      {(cs.data?.categories || []).map((c) => (
        <CategoryRow key={c.key} pid={pid} cat={c}
                     onSaved={() => { cs.reload(true); st.reload(true); }} />
      ))}

      {adding && (
        <Modal title="New category" onClose={() => setAdding(false)}
               sub="It applies from the next classify run — posts already labelled keep their label until you re-run.">
          <div className="field">
            <label htmlFor="ck">Key (letters, numbers, underscores)</label>
            <input id="ck" value={nc.key} autoFocus placeholder="against_bjp"
                   onChange={(e) => setNc({ ...nc, key: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="cn">Name</label>
            <input id="cn" value={nc.name} placeholder="Against BJP"
                   onChange={(e) => setNc({ ...nc, name: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="cd">What the model is told</label>
            <textarea id="cd" rows={3} value={nc.description}
                      placeholder="Criticises, attacks, opposes or mocks the BJP, its leaders, governments or policies."
                      onChange={(e) => setNc({ ...nc, description: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="cr">Precedence</label>
            <input id="cr" type="number" min="1" value={nc.rank}
                   onChange={(e) => setNc({ ...nc, rank: e.target.value })} />
          </div>
          {err && <div className="err">{err}</div>}
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setAdding(false)}>Cancel</button>
            <button className="btn btn-brand"
                    disabled={!nc.key.trim() || !nc.name.trim() || !nc.description.trim()}
                    onClick={addCat}>Create</button>
          </div>
        </Modal>
      )}
    </>
  );
}

export default function Collections({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const [tab, setTab] = useState("boards");
  const { data, error, loading, reload } = useApi(
    () => (pid ? api.collections(pid) : Promise.resolve({ collections: [] })), [pid]);
  // The vocabulary, so a board's posts can show their label by name.
  const cats = useApi(
    () => (pid ? api.labelCategories(pid) : Promise.resolve({ categories: [] })), [pid]);
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
        <Board c={open} pid={pid} cats={cats.data?.categories}
               onBack={() => setOpen(null)} onChanged={reload} />
      </>
    );
  }

  const boards = data?.collections || [];
  const autos = boards.filter((c) => c.auto);
  const mine = boards.filter((c) => !c.auto);

  const card = (c) => (
    <div className="panel" key={c.collection_id}>
      <div className="phead">
        <h3>{c.name}</h3>
        {c.auto ? <span className={`badge cat cat-${c.label_key}`}>auto</span> : null}
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
  );

  return (
    <>
      <PageHead title="Collections" onMenu={onMenu}
                sub={project ? `${project.name} — pinned posts, ready to hand off` : ""}>
        {tab === "boards" && (
          <button className="btn btn-brand" onClick={() => setCreating(true)}>+ New collection</button>
        )}
      </PageHead>

      <div className="tabs">
        <button className={`tab ${tab === "boards" ? "sel" : ""}`}
                onClick={() => setTab("boards")}>Boards</button>
        <button className={`tab ${tab === "labelling" ? "sel" : ""}`}
                onClick={() => setTab("labelling")}>Labelling</button>
      </div>

      {tab === "labelling" ? <Labelling pid={pid} /> : (
        <>
          {loading && !data && <Loading />}
          {error && <ErrorState error={error} retry={reload} />}
          {data && boards.length === 0 && (
            <Empty title="No collections yet">
              A collection is a board you pin posts onto — “Floods day 2”, “CM
              statements” — then download as CSV or hand to the desk. Classifying
              from the Live Feed fills one board per category on its own.
            </Empty>
          )}

          {autos.length > 0 && (
            <div className="feed-head">
              <h2>Label boards</h2>
              <span className="right" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                filled by the labeller
              </span>
            </div>
          )}
          {autos.map(card)}

          {mine.length > 0 && autos.length > 0 && (
            <div className="feed-head"><h2>Your boards</h2></div>
          )}
          {mine.map(card)}
        </>
      )}

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
               sub={confirming.auto
                 ? "The board goes away. Every post keeps its label, and the next classify run recreates the board."
                 : "The board goes away. Every post stays in your archive."}>
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
