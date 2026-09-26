// Settings — where the ADMIN knobs live, so that the working surfaces
import React, { useState } from "react";
import { api, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { ErrorState, Loading } from "../components/ui.jsx";

// The pager — which bot pages the admin, to which chat
function Pager({ pager, onChanged }) {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [name, setName] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (fn, okText) => {
    setBusy(true); setMsg("…");
    try {
      const r = await fn();
      if (r.error) setMsg(r.error);
      else { setMsg(okText(r)); setToken(""); onChanged(); }
    } catch (e) { setMsg(String(e.message || e)); }
    setBusy(false);
  };
  const save = () => run(() => api.pagerSave({ token, chat_id: chat, name }),
    (r) => `saved${r.found ? " · " + r.found : ""}`);
  const test = () => run(() => api.pagerTest(), () => "sent — check Telegram");

  if (!pager) return null;

  const ok = pager.ready;
  const line = pager.ready
    ? `Pages ${pager.admin}${pager.admin_chat ? ` (chat ${pager.admin_chat})` : ""} via `
      + (pager.own_bot
          ? `its own admin bot ${pager.token_hint}…`
          : `the delivery bot — it works, but a dedicated admin bot keeps alerts out of the delivery chat`)
    : pager.has_token
      ? "A bot token is set but there is no admin chat id yet. Press Start on the bot in Telegram, then Send test."
      : "Not set up — nobody is paged when an account needs a human.";

  return (
    <div className="panel">
      <div className="phead">
        <h3>Pager</h3>
        <span className="right">
          <span className={ok ? "st-good" : "st-warn"}>{ok ? "● paging" : "● not paging"}</span>
        </span>
      </div>
      <div style={{ padding: "0 14px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ color: "var(--ink-2)", fontSize: 13, lineHeight: 1.55 }}>{line}</div>
        <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.55 }}>
          The pager only messages you when a condition needs a <b>human</b>. Every
          ping says whether to open that account's browser — if it says
          “Browser: no”, there is nothing to clear and opening it would start a
          second session on the account.
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn btn-ghost" disabled={busy || !pager.has_token} onClick={test}>
            Send test
          </button>
          <button className="btn btn-ghost" disabled={busy} onClick={() => setOpen((v) => !v)}>
            {open ? "Close" : pager.has_token ? "Change" : "Set up"}
          </button>
        </div>
        {open && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
            <div className="field" style={{ flex: "2 1 280px", margin: 0 }}>
              <label>Admin bot token (from @BotFather)</label>
              <input value={token} onChange={(e) => setToken(e.target.value)} placeholder="123456789:AAH…" />
            </div>
            <div className="field" style={{ flex: "1 1 160px", margin: 0 }}>
              <label>Your chat id (blank = read from the bot after you press Start)</label>
              <input value={chat} onChange={(e) => setChat(e.target.value)} placeholder={pager.admin_chat || "auto"} />
            </div>
            <div className="field" style={{ flex: "1 1 120px", margin: 0 }}>
              <label>Your name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder={pager.admin || "Admin"} />
            </div>
            <button className="btn" disabled={busy || (!token && !chat && !name)} onClick={save}>Save</button>
          </div>
        )}
        {msg && <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{msg}</div>}
      </div>
    </div>
  );
}

// Secret storage
function Secrets({ pool }) {
  if (!pool) return null;
  const ok = !!pool.cipher_ready;
  return (
    <div className="panel">
      <div className="phead">
        <h3>Secret storage</h3>
        <span className="right">
          <span className={ok ? "st-good" : "st-crit"}>{ok ? "● encrypting" : "● not configured"}</span>
        </span>
      </div>
      <div style={{ padding: "0 14px 14px", color: ok ? "var(--ink-3)" : "var(--ink-2)", fontSize: 13, lineHeight: 1.6 }}>
        {ok ? (
          <>Account passwords, 2FA secrets and proxy URLs are encrypted at rest with
          <code> ACCOUNTS_SECRET_KEY</code>. They are never returned to this page — a
          proxy field shows only whether one is on file.</>
        ) : (
          <><b>Set <code>ACCOUNTS_SECRET_KEY</code> in .env on the server.</b> Without it
          the pool refuses to store passwords, 2FA secrets or proxy URLs, because the
          alternative is keeping them in plaintext.</>
        )}
      </div>
    </div>
  );
}

// System — what the server is actually running
function System({ diag, reload }) {
  if (!diag) return null;
  const svc = diag.services || {};
  const loop = diag.loop || null;
  const pass = diag.pass_running || null;
  const row = (k, v, cls = "") => (
    <div style={{ display: "flex", gap: 10, padding: "5px 0", fontSize: 13, borderBottom: "1px solid var(--line)" }}>
      <div style={{ width: 150, color: "var(--ink-3)", flexShrink: 0 }}>{k}</div>
      <div className={cls} style={{ wordBreak: "break-word" }}>{v}</div>
    </div>
  );
  return (
    <div className="panel">
      <div className="phead">
        <h3>System</h3>
        <span className="right">
          <button className="btn btn-ghost btn-sm" onClick={reload}>Refresh</button>
        </span>
      </div>
      <div style={{ padding: "0 14px 14px" }}>
        {row("Build", diag.git?.rev
          ? <span className="mono">{diag.git.rev}{diag.git.ref ? ` · ${diag.git.ref.replace("ref: ", "")}` : ""}</span>
          : <span style={{ color: "var(--ink-3)" }}>{diag.git?.error || "unknown"}</span>)}
        {row("instagrapi", <span className="mono">{diag.instagrapi || "—"}</span>)}
        {Object.entries(svc).map(([unit, state]) => {
          const good = /active|running/i.test(String(state));
          return (
            <div key={unit} style={{ display: "flex", gap: 10, padding: "5px 0", fontSize: 13, borderBottom: "1px solid var(--line)" }}>
              <div style={{ width: 150, color: "var(--ink-3)", flexShrink: 0 }} className="mono">{unit}</div>
              <div className={good ? "st-good" : "st-warn"}>{String(state)}</div>
            </div>
          );
        })}
        {row("Collection", diag.paused
          ? <span className="st-warn">paused from the dashboard</span>
          : <span className="st-good">running</span>)}
        {row("Pass right now", pass
          ? <>a pass is running — <span className="mono">{pass.who || "?"}</span> (pid {pass.pid || "?"})</>
          : <span style={{ color: "var(--ink-3)" }}>idle</span>)}
        {row("Loop heartbeat", loop
          ? <span className="mono">{JSON.stringify(loop).slice(0, 160)}</span>
          : <span style={{ color: "var(--ink-3)" }}>no heartbeat file yet</span>)}
        <div style={{ marginTop: 10, fontSize: 12.5, color: "var(--ink-3)", lineHeight: 1.6 }}>
          While a pass is running, the browser sign-in door is refused on purpose:
          two live sessions on one Instagram account is what Instagram restricts
          accounts for. Wait for the pass, or pause collection first.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
export default function Settings({ onMenu }) {
  const conds = useApi(() => api.deciderConditions(), []);
  const pool = useApi(() => api.pool(), []);
  const diag = useApi(() => api.igDiag(), []);
  const anyLoading = conds.loading && pool.loading && diag.loading;
  const reloadAll = () => { conds.reload(); pool.reload(); diag.reload(); };

  return (
    <>
      <PageHead title="Settings" onMenu={onMenu}
                sub="Admin configuration — global, set rarely, owned by you rather than by a project">
        <button className="btn btn-ghost" onClick={reloadAll}>Refresh</button>
      </PageHead>

      {anyLoading && !conds.data && !pool.data && <Loading />}
      {conds.error && !conds.data && <ErrorState error={conds.error} retry={conds.reload} />}

      <Pager pager={conds.data?.pager} onChanged={conds.reload} />
      <Secrets pool={pool.data} />
      <System diag={diag.data} reload={diag.reload} />

      <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.6, marginTop: 4 }}>
        Per-project wiring (streams, sheets, pausing one project's collection) lives
        in <b>Watchlists → Network &amp; settings</b>. Risk findings live in <b>Guard</b>.
      </div>
    </>
  );
}
