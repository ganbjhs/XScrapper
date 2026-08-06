// The pipe's outbound half, scoped to the current project: this project's
// own targets (created right here), plus the global ones from config.toml.
// "Behind: 0" is the whole point of the product.
import React, { useState } from "react";
import { api, fmtAgo, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

function AddTargetModal({ pid, onDone, onClose }) {
  const [kind, setKind] = useState("webhook");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secretEnv, setSecretEnv] = useState("");
  const [chat, setChat] = useState("");
  const [err, setErr] = useState("");

  const create = async () => {
    setErr("");
    try {
      await api.createDeliveryTarget({
        project: pid, kind, name, url, secret_env: secretEnv, chat_id: chat,
      });
      onDone();
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <Modal title="New delivery target" onClose={onClose}
           sub="Only posts collected by THIS project's streams are sent here. A new target starts from now, never the archive.">
      <div className="field">
        <label htmlFor="dkind">Type</label>
        <select id="dkind" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="webhook">Webhook (Watch-Tower or any system)</option>
          <option value="telegram">Telegram chat/channel</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="dname">Name</label>
        <input id="dname" value={name} onChange={(e) => setName(e.target.value)}
               placeholder={kind === "webhook" ? "Watch-Tower" : "War-room group"} />
      </div>
      {kind === "webhook" ? (
        <>
          <div className="field">
            <label htmlFor="durl">URL</label>
            <input id="durl" value={url} onChange={(e) => setUrl(e.target.value)}
                   placeholder="https://app.watch-tower.in/hooks/tweets" />
          </div>
          <div className="field">
            <label htmlFor="dsec">Secret — the NAME of an .env variable</label>
            <input id="dsec" value={secretEnv}
                   onChange={(e) => setSecretEnv(e.target.value.toUpperCase())}
                   placeholder="WEBHOOK_SECRET_ISUPPORT" />
            <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
              Put the actual secret in <code>.env</code> on the server under
              this name and share it with the receiver — deliveries are signed
              with it. The secret itself is never stored in the database.
            </div>
          </div>
        </>
      ) : (
        <div className="field">
          <label htmlFor="dchat">Chat id</label>
          <input id="dchat" value={chat} onChange={(e) => setChat(e.target.value)}
                 placeholder="-1001234567890 or @channel" />
        </div>
      )}
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={!name.trim()} onClick={create}>
          Create target
        </button>
      </div>
    </Modal>
  );
}

function TargetPanel({ t, reload }) {
  const own = t.target_id != null;
  return (
    <div className="panel">
      <div className="phead">
        <h3>
          <span className={`dot${!t.enabled ? " off" : t.failures ? " bad" : t.behind ? " warn" : ""}`}
                style={{ display: "inline-block", marginRight: 9 }} />
          {t.name}
        </h3>
        <span className="right">
          {t.kind} → {t.url}
          {t.scope === "global" ? " · global (config.toml)" : ""}
        </span>
      </div>
      {own && t.kind === "webhook" && !t.secret_ready && (
        <div className="kv"><span>Not sending</span>
          <b className="st-crit">{t.secret_env} is not set in .env on the server</b></div>
      )}
      {!t.started ? (
        <div className="kv">
          <span>Waiting for its first delivery</span>
          <b>starts from now, not the archive</b>
        </div>
      ) : (
        <>
          <div className="kv"><span>Cursor behind</span>
            <b className={t.behind ? "st-warn" : "st-good"}>
              {t.behind ? `${fmtN(t.behind)} posts` : "✓ 0 — in sync"}</b></div>
          <div className="kv"><span>Delivered (lifetime)</span><b>{fmtN(t.sent)}</b></div>
          <div className="kv"><span>Last success</span>
            <b>{t.last_ok_ms ? fmtAgo(t.last_ok_ms) : "never"}</b></div>
          {t.failures > 0 && (
            <div className="kv"><span>Failing</span>
              <b className="st-crit">{t.failures}× — {t.last_error}</b></div>
          )}
          {t.streams?.length > 0 && (
            <div className="kv"><span>Scoped to streams</span><b>{t.streams.join(", ")}</b></div>
          )}
        </>
      )}
      {own && (
        <div className="filters" style={{ marginTop: 10, marginBottom: 0 }}>
          <button className="btn btn-ghost btn-sm"
                  onClick={async () => {
                    await api.updateDeliveryTarget({ target_id: t.target_id, enabled: !t.enabled });
                    reload();
                  }}>
            {t.enabled ? "Pause" : "Resume"}
          </button>
          <span style={{ flex: 1 }} />
          <button className="btn btn-danger btn-sm"
                  onClick={async () => { await api.removeDeliveryTarget(t.target_id); reload(); }}>
            Delete
          </button>
        </div>
      )}
    </div>
  );
}

export default function Delivery({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const { data, error, loading, reload } = useApi(
    () => api.delivery(pid), [pid], { every: 10_000 });
  const [adding, setAdding] = useState(false);

  const targets = data?.targets || [];
  const own = targets.filter((t) => t.scope !== "global");
  const global = targets.filter((t) => t.scope === "global");

  return (
    <>
      <PageHead title="Delivery" onMenu={onMenu}
                sub={project ? `${project.name} — where this project's posts go` : ""}>
        <button className="btn btn-brand" onClick={() => setAdding(true)}>+ New target</button>
      </PageHead>

      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && targets.length === 0 && (
        <Empty title="Nothing is delivered anywhere yet">
          Add a target — a Watch-Tower webhook or a Telegram chat — and every
          post this project collects is sent there, seconds after collection.
        </Empty>
      )}

      {own.map((t) => <TargetPanel key={t.label} t={t} reload={reload} />)}

      {global.length > 0 && (
        <>
          <div className="feed-head" style={{ marginTop: 18 }}>
            <h2>Global targets</h2>
            <span className="right">from config.toml / per-stream Telegram — receive ALL projects</span>
          </div>
          {global.map((t) => <TargetPanel key={t.label} t={t} reload={reload} />)}
        </>
      )}

      {targets.length > 0 && (
        <div className="state" style={{ textAlign: "left" }}>
          <b>How delivery works</b>
          Position is a cursor in the database, not a queue — a receiver that
          goes down catches up by itself when it returns, and nothing is ever
          lost. Webhook payloads are HMAC-signed and include full media.
          Receivers de-duplicate on <code>tweet_id</code>.
        </div>
      )}

      {adding && pid && (
        <AddTargetModal pid={pid} onDone={reload} onClose={() => setAdding(false)} />
      )}
    </>
  );
}
