// Account Control Panel — manage the scraper accounts of all three platforms
// from one place: add / edit / remove, see status, promote a backup, force
// failover, refresh backup codes, preview the current TOTP, and (next step)
// sign in on the server IP. The managed pool lives in store_accounts; the live
// session health (is it actually collecting?) is enriched in from the existing
// X / Instagram status endpoints so nothing you already run disappears.
import React, { useState } from "react";
import { api, fmtAgo, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

const PLATS = [["x", "X"], ["ig", "Instagram"], ["fb", "Facebook"]];
const BADGE = { x: "platform-x", ig: "platform-ig", fb: "platform-fb" };
const BADGE_TXT = { x: "X", ig: "IG", fb: "FB" };

const STATUS = {
  active: { dot: "", text: "Active", cls: "st-good" },
  backup: { dot: " off", text: "Backup", cls: "" },
  needs_login: { dot: " warn", text: "Needs login", cls: "st-warn" },
  quarantined: { dot: " warn", text: "Quarantined", cls: "st-warn" },
  dead: { dot: " bad", text: "Dead", cls: "st-crit" },
};

// ---------------------------------------------------------------------------
// Add / edit / backup-code modals
// ---------------------------------------------------------------------------

function AddModal({ onDone, onClose }) {
  const [f, setF] = useState({
    platform: "x", label: "", login: "", password: "",
    totp_secret: "", backup_codes: "", proxy_id: "", notes: "",
  });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const save = async () => {
    setBusy(true); setErr("");
    try {
      await api.poolAdd({ ...f, proxy_id: f.proxy_id || null });
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title="Add account" onClose={onClose}
           sub="Enters the pool as a warm backup. Secrets are encrypted at rest.">
      <div className="field">
        <label>Platform</label>
        <select value={f.platform} onChange={set("platform")}>
          <option value="x">X (Twitter)</option>
          <option value="ig">Instagram</option>
          <option value="fb">Facebook</option>
        </select>
      </div>
      <div className="field">
        <label>Label</label>
        <input value={f.label} autoFocus onChange={set("label")} placeholder="e.g. fb_backup_2" />
      </div>
      <div className="field">
        <label>Login (username / email)</label>
        <input value={f.login} onChange={set("login")} placeholder="account@example.com" />
      </div>
      <div className="field">
        <label>Password</label>
        <input type="password" value={f.password} onChange={set("password")}
               placeholder="stored encrypted" />
      </div>
      <div className="field">
        <label>TOTP secret (authenticator setup key)</label>
        <input value={f.totp_secret} onChange={set("totp_secret")}
               placeholder="paste the setup key — spaces ok" />
      </div>
      <div className="field">
        <label>Backup codes — one per line (optional)</label>
        <textarea rows="3" value={f.backup_codes} onChange={set("backup_codes")}
                  placeholder={"11112222\n33334444"} />
      </div>
      <div className="field">
        <label>Proxy / IP id (optional)</label>
        <input value={f.proxy_id} onChange={set("proxy_id")} placeholder="e.g. resi-in-01" />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !f.label.trim() || !f.login.trim()}
                onClick={save}>Add to pool</button>
      </div>
    </Modal>
  );
}

function EditModal({ a, onDone, onClose }) {
  const [f, setF] = useState({
    label: a.label, login: a.login, password: "", totp_secret: "",
    proxy_id: a.proxy_id || "", notes: "",
  });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const save = async () => {
    setBusy(true); setErr("");
    try {
      const body = { account_id: a.account_id, label: f.label, login: f.login,
                     proxy_id: f.proxy_id || null };
      if (f.password) body.password = f.password;         // blank = keep
      if (f.totp_secret) body.totp_secret = f.totp_secret; // blank = keep
      await api.poolUpdate(body);
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`Edit ${a.label}`} onClose={onClose}
           sub="Leave password / TOTP blank to keep the current one.">
      <div className="field"><label>Label</label>
        <input value={f.label} onChange={set("label")} /></div>
      <div className="field"><label>Login</label>
        <input value={f.login} onChange={set("login")} /></div>
      <div className="field"><label>New password (blank = keep)</label>
        <input type="password" value={f.password} onChange={set("password")} /></div>
      <div className="field"><label>New TOTP secret (blank = keep)</label>
        <input value={f.totp_secret} onChange={set("totp_secret")} /></div>
      <div className="field"><label>Proxy / IP id</label>
        <input value={f.proxy_id} onChange={set("proxy_id")} placeholder="none" /></div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !f.label.trim()} onClick={save}>
          Save changes
        </button>
      </div>
    </Modal>
  );
}

function CodesModal({ a, onDone, onClose }) {
  const [codes, setCodes] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true); setErr("");
    try {
      await api.poolBackupCodes(a.account_id, codes);
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`Backup codes — ${a.label}`} onClose={onClose}
           sub={`${a.backup_codes_left} unused now. Pasting a new set REPLACES the old one.`}>
      <div className="field">
        <label>One-time recovery codes — one per line</label>
        <textarea rows="6" value={codes} autoFocus onChange={(e) => setCodes(e.target.value)}
                  placeholder={"paste the fresh set from the account's 2FA settings"} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !codes.trim()} onClick={save}>
          Save codes
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// One account card
// ---------------------------------------------------------------------------

function AccountCard({ a, live, onChanged }) {
  const [msg, setMsg] = useState("");
  const [modal, setModal] = useState(null);
  const s = STATUS[a.status] || STATUS.backup;

  const act = async (fn, okMsg) => {
    setMsg("…");
    try {
      const r = await fn();
      if (r && r.ok === false && r.todo) setMsg(r.todo);
      else setMsg(okMsg || "done");
      onChanged();
    } catch (e) { setMsg(String(e.message || e)); }
  };

  const remove = () => {
    if (!confirm(`Remove ${a.label}? This deletes the account from the pool.`)) return;
    act(() => api.poolRemove(a.account_id), "removed");
  };
  const promote = () => act(() => api.poolPromote(a.account_id), "promoted to active");
  const login = () => act(() => api.poolLogin(a.account_id));
  const quarantine = () => act(() => api.poolStatus(a.account_id, "quarantined"), "quarantined");
  const revive = () => act(() => api.poolStatus(a.account_id, "backup"), "returned to pool");
  const showCode = async () => {
    setMsg("…");
    try { const r = await api.poolTotp(a.account_id); setMsg(r.code ? `TOTP now: ${r.code}` : "no TOTP set"); }
    catch (e) { setMsg(String(e.message || e)); }
  };

  return (
    <div className="panel">
      <div className="phead">
        <h3>
          <span className={`dot${s.dot}`} style={{ display: "inline-block", marginRight: 9 }} />
          {a.label}
        </h3>
        <span className={`badge ${BADGE[a.platform]}`} style={{ marginLeft: 4 }}>{BADGE_TXT[a.platform]}</span>
        <b className={s.cls} style={{ marginLeft: 8, fontSize: 12.5 }}>{s.text}</b>
        <span className="right">
          {a.proxy_id ? `IP: ${a.proxy_id}` : "no proxy"} · {a.has_totp ? "TOTP" : "no 2FA"} · {a.backup_codes_left} codes
        </span>
      </div>

      <div className="kv"><span>login</span><b>{a.login}</b></div>
      {live && (
        <div className="kv"><span>live session</span>
          <b className={live.active ? "st-good" : "st-crit"}>
            {live.active ? "signed in · collecting" : "not signed in"}
            {live.requests != null ? ` · ${live.requests} requests` : ""}
          </b>
        </div>
      )}
      <div className="kv"><span>last success</span><b>{a.last_success_at ? fmtAgo(a.last_success_at) : "—"}</b></div>
      {a.health && <div className="kv"><span>health</span><b className="st-warn">{a.health}</b></div>}
      {msg && <div className="kv"><span>note</span><b>{msg}</b></div>}

      <div className="cactions">
        {a.status !== "active" && <button onClick={promote}>Promote</button>}
        <button onClick={login}>Login now</button>
        <button onClick={showCode}>Show TOTP</button>
        <button onClick={() => setModal("edit")}>Edit</button>
        <button onClick={() => setModal("codes")}>Codes ({a.backup_codes_left})</button>
        {a.status !== "quarantined" && a.status !== "dead"
          ? <button onClick={quarantine}>Quarantine</button>
          : <button onClick={revive}>Return to pool</button>}
        <button onClick={remove} style={{ color: "var(--critical)" }}>Remove</button>
      </div>

      {modal === "edit" && <EditModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
      {modal === "codes" && <CodesModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One platform section
// ---------------------------------------------------------------------------

function PlatformSection({ platform, title, summary, accounts, liveFor, onChanged }) {
  const failover = async () => {
    if (!confirm(`Force failover on ${title}? The active account is quarantined and the next backup takes over.`)) return;
    const proxy = prompt("Fresh proxy/IP id for the promoted account (recommended — leave blank to keep its own):", "");
    try {
      const r = await api.poolFailover(platform, proxy || null);
      alert(r.promoted ? `Promoted ${r.promoted}.` : "No backup available to promote.");
      onChanged();
    } catch (e) { alert(String(e.message || e)); }
  };

  return (
    <>
      <div className="feed-head" style={{ marginTop: 18 }}>
        <h2>{title}</h2>
        <span className="right">
          {summary.active ? <>active: <b>{summary.active}</b> · </> : "no active account · "}
          {summary.backups} backup{summary.backups === 1 ? "" : "s"}
          {summary.active && summary.backups > 0 && (
            <button className="btn btn-ghost btn-sm" style={{ marginLeft: 10 }} onClick={failover}>
              Force failover
            </button>
          )}
        </span>
      </div>

      {summary.low && accounts.length > 0 && (
        <div className="banner-crit" style={{ borderLeftColor: "var(--warning)" }}>
          <b style={{ color: "var(--warn-text)" }}>Pool low.</b> Only {summary.backups} backup
          {summary.backups === 1 ? "" : "s"} left for {title} — add another so a ban never causes an outage.
        </div>
      )}

      {accounts.length === 0
        ? <Empty title={`No ${title} accounts yet`}>Use “Add account” to put one in the pool.</Empty>
        : accounts.map((a) => (
            <AccountCard key={a.account_id} a={a} live={liveFor(a)} onChanged={onChanged} />
          ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

export default function Accounts({ onMenu }) {
  const pool = useApi(() => api.pool(), [], { every: 30_000 });
  const liveX = useApi(() => api.status(), [], { every: 30_000 });
  const liveIg = useApi(() => api.igStatus(), []);
  const [adding, setAdding] = useState(false);

  const reload = () => { pool.reload(); liveX.reload(); liveIg.reload(); };

  // Match a managed account to its live session (best-effort, by label/username).
  const liveFor = (a) => {
    if (a.platform === "x") {
      const hit = (liveX.data?.accounts || []).find(
        (r) => r.label === a.label || r.username === a.login || r.username === a.label);
      return hit ? { active: hit.active, requests: hit.requests } : null;
    }
    if (a.platform === "ig") {
      const hit = (liveIg.data?.accounts || []).find(
        (r) => r.username === a.login || r.label === a.label || r.username === a.label);
      return hit ? { active: hit.active, requests: hit.requests } : null;
    }
    return null;
  };

  const plats = pool.data?.platforms || {};
  const accounts = pool.data?.accounts || [];

  return (
    <>
      <PageHead title="Accounts & Sessions" onMenu={onMenu}
                sub="One pool per platform · one active, the rest warm backups · failover on ban">
        <button className="btn btn-brand" onClick={() => setAdding(true)}>+ Add account</button>
      </PageHead>

      {pool.loading && !pool.data && <Loading />}
      {pool.error && !pool.data && <ErrorState error={pool.error} retry={pool.reload} />}

      {pool.data && !pool.data.cipher_ready && (
        <div className="banner-crit">
          <b>Set <code>ACCOUNTS_SECRET_KEY</code> in .env.</b> Without it, account passwords and
          2FA secrets can’t be stored — the panel refuses to keep them in plaintext.
        </div>
      )}

      {pool.data && PLATS.map(([p, title]) => (
        <PlatformSection
          key={p}
          platform={p}
          title={title}
          summary={plats[p] || { active: null, backups: 0, low: false }}
          accounts={accounts.filter((a) => a.platform === p)}
          liveFor={liveFor}
          onChanged={reload}
        />
      ))}

      {adding && <AddModal onDone={reload} onClose={() => setAdding(false)} />}
    </>
  );
}
