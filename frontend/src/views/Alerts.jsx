// Velocity alerts: "ping Telegram when this is moving faster than usual."
// Rules are counts over collected posts — no AI, no sentiment.
import React, { useState } from "react";
import { api, fmtAgo, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

function CreateModal({ pid, watchlists, onDone, onClose }) {
  const [wid, setWid] = useState("");
  const [threshold, setThreshold] = useState("3");
  const [minPosts, setMinPosts] = useState("10");
  const [chat, setChat] = useState("");
  const [err, setErr] = useState("");

  const create = async () => {
    setErr("");
    try {
      await api.createAlert({
        project: pid,
        watchlist_id: wid ? Number(wid) : null,
        threshold: Number(threshold) || 3,
        min_posts: Number(minPosts) || 10,
        tg_chat_id: chat,
      });
      onDone();
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <Modal title="New alert" onClose={onClose}
           sub="Fires when the last hour runs well above the usual pace — and goes quiet for 30 minutes after each ping.">
      <div className="field">
        <label htmlFor="ascope">Watch</label>
        <select id="ascope" value={wid} onChange={(e) => setWid(e.target.value)}>
          <option value="">The whole project</option>
          {watchlists.map((w) => (
            <option key={w.watchlist_id} value={w.watchlist_id}>
              Watchlist: {w.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="athr">Sensitivity — “× the usual pace”</label>
        <select id="athr" value={threshold} onChange={(e) => setThreshold(e.target.value)}>
          <option value="2">2× — sensitive, pings more often</option>
          <option value="3">3× — balanced (recommended)</option>
          <option value="5">5× — only real surges</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="amin">Never fire under this many posts/hour</label>
        <input id="amin" inputMode="numeric" value={minPosts}
               onChange={(e) => setMinPosts(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="achat">Telegram chat (blank = your default chat)</label>
        <input id="achat" value={chat} placeholder="-1001234567890 or @channel"
               onChange={(e) => setChat(e.target.value)} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" onClick={create}>Create alert</button>
      </div>
    </Modal>
  );
}

export default function Alerts({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const { data, error, loading, reload } = useApi(
    () => (pid ? api.alerts(pid) : Promise.resolve({ alerts: [] })), [pid],
    { every: 30_000 });
  const wls = useApi(
    () => (pid ? api.watchlists(pid) : Promise.resolve({ watchlists: [] })), [pid]);
  const [creating, setCreating] = useState(false);

  const toggle = async (a) => {
    await api.updateAlert({ alert_id: a.alert_id, enabled: !a.enabled });
    reload();
  };

  return (
    <>
      <PageHead title="Alerts" onMenu={onMenu}
                sub={project ? `${project.name} — Telegram pings when something is moving` : ""}>
        <button className="btn btn-brand" onClick={() => setCreating(true)}>+ New alert</button>
      </PageHead>

      {data && !data.telegram_ready && (
        <div className="banner-crit" role="alert">
          <b>Telegram is not set up.</b> Alerts have nowhere to send — add
          <code style={{ margin: "0 6px" }}>TELEGRAM_BOT_TOKEN</code> to .env
          (and a default <code>TELEGRAM_CHAT_ID</code>, or set a chat per alert).
        </div>
      )}

      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && data.alerts.length === 0 && (
        <Empty title="No alerts yet">
          An alert watches the pace of a watchlist (or the whole project) and
          pings your Telegram when the last hour runs far above normal — so a
          developing story finds you.
        </Empty>
      )}

      {(data?.alerts || []).map((a) => (
        <div className="panel" key={a.alert_id}>
          <div className="phead">
            <h3>
              <span className={`dot${a.enabled ? "" : " off"}`}
                    style={{ display: "inline-block", marginRight: 9 }} />
              {a.watchlist_name ? `Watchlist: ${a.watchlist_name}` : "Whole project"}
            </h3>
            <span className="right">
              fires at {a.threshold}× usual pace · min {a.min_posts}/hour
            </span>
          </div>
          <div className="kv">
            <span>Sends to</span>
            <b>{a.tg_chat_id || (data.default_chat ? "default Telegram chat" : "— nothing set")}</b>
          </div>
          <div className="kv">
            <span>Last fired</span>
            <b>{a.last_fired_ms ? fmtAgo(a.last_fired_ms) : "never"}</b>
          </div>
          <div className="filters" style={{ marginBottom: 0, marginTop: 10 }}>
            <button className="btn btn-ghost btn-sm" onClick={() => toggle(a)}>
              {a.enabled ? "Pause" : "Resume"}
            </button>
            <span style={{ flex: 1 }} />
            <button className="btn btn-danger btn-sm"
                    onClick={async () => { await api.removeAlert(a.alert_id); reload(); }}>
              Delete
            </button>
          </div>
        </div>
      ))}

      {creating && pid && (
        <CreateModal pid={pid} watchlists={wls.data?.watchlists || []}
                     onDone={reload} onClose={() => setCreating(false)} />
      )}
    </>
  );
}
