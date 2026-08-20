// The pipe's outbound half, scoped to the current project: this project's
// own targets (created right here), plus the global ones from config.toml.
// "Behind: 0" is the whole point of the product.
import React, { useEffect, useState } from "react";
import { api, fmtAgo, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

function Copyable({ label, value, rows }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      return;                       // an insecure origin blocks the API
    }
    setDone(true);
    setTimeout(() => setDone(false), 1500);
  };
  return (
    <div className="field">
      <label>{label}</label>
      <textarea readOnly value={value} rows={rows || 3} spellCheck="false"
                onFocus={(e) => e.target.select()}
                style={{ width: "100%", fontFamily: "ui-monospace, monospace",
                         fontSize: 12, whiteSpace: "pre", overflowX: "auto" }} />
      <button className="btn btn-ghost btn-sm" style={{ marginTop: 6 }}
              onClick={copy}>
        {done ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function AddTargetModal({ pid, serviceAccount, onDone, onClose }) {
  const [kind, setKind] = useState("webhook");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secretEnv, setSecretEnv] = useState("");
  const [chat, setChat] = useState("");
  const [sheetUrl, setSheetUrl] = useState("");
  const [sheetTab, setSheetTab] = useState("Sheet1");
  // Apps Script by default: it needs no Google Cloud project and no key on
  // the server, which is the difference between "set this up now" and "set
  // this up one day".
  const [sheetMode, setSheetMode] = useState("script");
  // The sheet token's .env variable is its own state, NOT shared with the
  // webhook secret above — one form, two unrelated credentials.
  const [sheetEnv, setSheetEnv] = useState("");
  const [script, setScript] = useState(null);
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const [err, setErr] = useState("");

  // Ask the server what token this variable should carry.
  //
  // It answers with the value ALREADY in .env when there is one, and mints a
  // new one only when there is not (or when `rotate` is passed). That matters:
  // an earlier version generated a token per call, so reopening this form
  // quietly replaced the token of a deployment that was already working, and
  // made a correct .env look wrong.
  const loadScript = async (opts = {}) => {
    try {
      const r = await api.sheetScript({
        name, secret_env: sheetEnv, rotate: !!opts.rotate,
      });
      if (r.error) return;
      setScript(r);
      setCheck(null);
      if (!sheetEnv) setSheetEnv(r.secret_env);
    } catch {
      /* leave the panel as it was */
    }
  };

  useEffect(() => {
    if (kind !== "sheet" || sheetMode !== "script") return undefined;
    // Debounced: this refires while the variable name is being typed.
    const t = setTimeout(() => { loadScript(); }, 350);
    return () => clearTimeout(t);
  }, [kind, sheetMode, sheetEnv]);  // eslint-disable-line react-hooks/exhaustive-deps

  const testSheet = async () => {
    setChecking(true);
    setCheck(null);
    try {
      const r = await api.testSheet({
        sheet_mode: sheetMode, sheet_id: sheetUrl, sheet_tab: sheetTab,
        url, secret_env: sheetMode === "script" ? sheetEnv : secretEnv,
        token: script?.token,
      });
      setCheck(r.error ? `\u2717 ${r.error}`
                       : "\u2713 Connected — the header row is ready");
    } catch (e) {
      setCheck(`\u2717 ${String(e.message || e)}`);
    } finally {
      setChecking(false);
    }
  };

  const create = async () => {
    setErr("");
    try {
      await api.createDeliveryTarget({
        project: pid, kind, name, url, chat_id: chat,
        secret_env: kind === "sheet" ? sheetEnv : secretEnv,
        sheet_id: sheetUrl, sheet_tab: sheetTab, sheet_mode: sheetMode,
      });
      onDone();
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const ready = name.trim() && (
    kind === "webhook" ? url.trim() && secretEnv.trim()
      : kind === "telegram" ? chat.trim()
        : sheetMode === "script" ? url.trim() && sheetEnv.trim()
          : sheetUrl.trim());

  return (
    <Modal title="New delivery target" onClose={onClose}
           sub="Only posts collected by THIS project's streams are sent here. A new target starts from now, never the archive.">
      <div className="field">
        <label htmlFor="dkind">Type</label>
        <select id="dkind" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="webhook">Webhook (Watch-Tower or any system)</option>
          <option value="telegram">Telegram chat/channel</option>
          <option value="sheet">Google Sheet</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="dname">Name</label>
        <input id="dname" value={name} onChange={(e) => setName(e.target.value)}
               placeholder={kind === "webhook" ? "Watch-Tower"
                 : kind === "sheet" ? "Daily posts sheet" : "War-room group"} />
      </div>

      {kind === "webhook" && (
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
      )}

      {kind === "telegram" && (
        <div className="field">
          <label htmlFor="dchat">Chat id</label>
          <input id="dchat" value={chat} onChange={(e) => setChat(e.target.value)}
                 placeholder="-1001234567890 or @channel" />
        </div>
      )}

      {kind === "sheet" && (
        <>
          <div className="field">
            <label htmlFor="dmode">How the sheet is reached</label>
            <select id="dmode" value={sheetMode}
                    onChange={(e) => { setSheetMode(e.target.value); setCheck(null); }}>
              <option value="script">
                Apps Script — no Google Cloud project (easiest)
              </option>
              <option value="service_account">
                Service account — Sheets API, needs a Google Cloud key
              </option>
            </select>
          </div>

          <div className="field">
            <label htmlFor="dtab">Tab name</label>
            <input id="dtab" value={sheetTab}
                   onChange={(e) => setSheetTab(e.target.value)}
                   placeholder="Sheet1" />
            <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
              Columns <code>date</code>, <code>link</code>, <code>text</code>,{" "}
              <code>media</code> are written as a header row if the tab is
              empty. Posts are appended underneath, newest at the bottom.
            </div>
          </div>
        </>
      )}

      {kind === "sheet" && sheetMode === "script" && (
        <>
          <div className="field">
            <label htmlFor="dsenv">Token — the NAME of the .env variable</label>
            <input id="dsenv" value={sheetEnv}
                   onChange={(e) => setSheetEnv(e.target.value.toUpperCase())}
                   placeholder="SHEET_TOKEN_MAIN" />
            <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
              The token itself is never stored in the database — only this
              name, so the value can be rotated without touching a target.
            </div>
          </div>

          {script?.already_set ? (
            <div className="state" style={{ textAlign: "left", marginTop: 4 }}>
              <b className="st-good">{script.secret_env} is already set on this server</b>
              The script below carries that same token, so a deployment you have
              already made is still valid — skip to the web app URL. Paste the
              script again only if you changed it.
            </div>
          ) : (
            <div className="state" style={{ textAlign: "left", marginTop: 4 }}>
              <b>{script?.rotated ? "New token generated" : "Three steps, about two minutes"}</b>
              {script?.rotated
                ? "The old one stops working the moment you redeploy. Paste the script again, update .env, and restart."
                : "The script runs inside your own sheet, as you — so there is no Google Cloud project, no key file, and nothing to share."}
            </div>
          )}

          {script ? (
            <>
              <Copyable rows={12} value={script.code}
                        label="1. In the sheet: Extensions → Apps Script. Replace everything with this, then Save." />
              <div className="field">
                <label>2. Deploy → New deployment → Web app</label>
                <div style={{ fontSize: 12, marginTop: 2 }}>
                  Set <b>Execute as: Me</b> and <b>Who has access: Anyone</b>.
                  Google will ask you to authorise it once. Copy the{" "}
                  <code>/exec</code> URL it gives you back and paste it below.
                  <div style={{ color: "var(--ink-3)", marginTop: 4 }}>
                    “Anyone” is safe here because the token above is what the
                    script actually checks.
                  </div>
                </div>
              </div>
              <Copyable rows={2} value={script.env_line}
                        label={script.already_set
                          ? "3. This is what the server already holds — .env needs no change."
                          : "3. Add this line to .env on the server, then restart the collector."} />
              <div className="filters" style={{ marginTop: 0, marginBottom: 6 }}>
                <button className="btn btn-ghost btn-sm"
                        onClick={() => loadScript({ rotate: true })}>
                  Generate a new token
                </button>
                <span style={{ color: "var(--ink-3)", fontSize: 12 }}>
                  Only if the current one leaked or was lost — it invalidates the
                  deployed script until you paste and redeploy it.
                </span>
              </div>
            </>
          ) : (
            <div className="field">
              <label>Preparing your script…</label>
            </div>
          )}

          <div className="field">
            <label htmlFor="dexec">Web app URL</label>
            <input id="dexec" value={url}
                   onChange={(e) => setUrl(e.target.value)}
                   placeholder="https://script.google.com/macros/s/…/exec" />
          </div>
        </>
      )}

      {kind === "sheet" && sheetMode === "service_account" && (
        <>
          <div className="field">
            <label htmlFor="dsheet">Google Sheet link</label>
            <input id="dsheet" value={sheetUrl}
                   onChange={(e) => setSheetUrl(e.target.value)}
                   placeholder="https://docs.google.com/spreadsheets/d/…" />
            <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
              Paste the whole address from your browser — the id is picked out
              of it.
            </div>
          </div>
          <div className="field">
            <label>Share the sheet first</label>
            {serviceAccount ? (
              <>
                <div style={{ fontSize: 12, marginTop: 2 }}>
                  In the sheet, click <b>Share</b> and give <b>Editor</b> access to
                </div>
                <code style={{ display: "block", marginTop: 6, wordBreak: "break-all" }}>
                  {serviceAccount}
                </code>
              </>
            ) : (
              <div className="err" style={{ marginTop: 2 }}>
                No service-account key on the server yet — set{" "}
                <code>GOOGLE_SHEETS_CREDENTIALS</code> in <code>.env</code> to
                the path of the downloaded JSON key, then restart. If you would
                rather not make a Google Cloud project, switch the option above
                to Apps Script.
              </div>
            )}
          </div>
        </>
      )}

      {kind === "sheet" && (
        <div className="filters" style={{ marginTop: 0, marginBottom: 6 }}>
          <button className="btn btn-ghost btn-sm"
                  disabled={checking
                    || !(sheetMode === "script" ? url.trim() : sheetUrl.trim())}
                  onClick={testSheet}>
            {checking ? "Checking…" : "Check access"}
          </button>
          {check && (
            <span className={check.startsWith("\u2713") ? "st-good" : "st-crit"}
                  style={{ fontWeight: 600 }}>{check}</span>
          )}
        </div>
      )}

      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={!ready} onClick={create}>
          Create target
        </button>
      </div>
    </Modal>
  );
}

function BackfillModal({ t, onClose }) {
  const [mode, setMode] = useState("recent");
  const [since, setSince] = useState("24h");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [limit, setLimit] = useState("20");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const send = async () => {
    setBusy(true);
    setResult(null);
    try {
      const body = { target_id: t.target_id, limit: Number(limit) || 20 };
      if (mode === "dates") {
        body.from_date = fromDate;
        body.to_date = toDate || undefined;
      } else {
        body.since = since;
      }
      const r = await api.deliveryBackfill(body);
      setResult(`✓ Sent ${r.sent} post${r.sent === 1 ? "" : "s"}${r.note ? ` — ${r.note}` : ""}`);
    } catch (e) {
      setResult(`✗ ${String(e.message || e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={`Send past posts to “${t.name}”`} onClose={onClose}
           sub="A one-time send of already-collected posts, oldest first. Live delivery is untouched — nothing gets duplicated.">
      <div className="field">
        <label htmlFor="bfmode">Pick posts by</label>
        <select id="bfmode" value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="recent">The last… (quick presets)</option>
          <option value="dates">Exact dates (e.g. all of July)</option>
        </select>
      </div>
      {mode === "recent" ? (
        <div className="field">
          <label htmlFor="bfsince">Posts from the last…</label>
          <select id="bfsince" value={since} onChange={(e) => setSince(e.target.value)}>
            <option value="1h">1 hour</option><option value="6h">6 hours</option>
            <option value="12h">12 hours</option><option value="24h">24 hours</option>
            <option value="48h">48 hours</option><option value="7d">7 days</option>
            <option value="30d">30 days</option>
          </select>
        </div>
      ) : (
        <>
          <div className="field">
            <label htmlFor="bffrom">From (posted date, inclusive)</label>
            <input id="bffrom" type="date" value={fromDate}
                   onChange={(e) => setFromDate(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="bfto">To (inclusive — leave blank for “until now”)</label>
            <input id="bfto" type="date" value={toDate}
                   onChange={(e) => setToDate(e.target.value)} />
          </div>
        </>
      )}
      <div className="field">
        <label htmlFor="bflimit">
          {t.kind === "sheet"
            ? "At most (one append, max 2000 rows)"
            : "At most (Telegram allows ~20/min; max 50)"}
        </label>
        <input id="bflimit" inputMode="numeric" value={limit}
               onChange={(e) => setLimit(e.target.value)} />
      </div>
      {result && (
        <div style={{ marginTop: 12, fontWeight: 600 }}
             className={result.startsWith("✓") ? "st-good" : "st-crit"}>
          {result}
        </div>
      )}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
        <button className="btn btn-brand" disabled={busy || (mode === "dates" && !fromDate)}
                onClick={send}>
          {busy ? (t.kind === "sheet" ? "Writing rows…" : "Sending… (paced for Telegram)")
                : "Send"}
        </button>
      </div>
    </Modal>
  );
}

function TargetPanel({ t, reload }) {
  const own = t.target_id != null;
  const [backfilling, setBackfilling] = useState(false);
  return (
    <div className="panel">
      <div className="phead">
        <h3>
          <span className={`dot${!t.enabled ? " off" : t.failures ? " bad" : t.behind ? " warn" : ""}`}
                style={{ display: "inline-block", marginRight: 9 }} />
          {t.name}
        </h3>
        <span className="right">
          {t.kind === "sheet"
            ? `sheet → ${t.sheet_mode === "service_account"
                ? "Sheets API" : "Apps Script"}`
            : `${t.kind} → ${t.url}`}
          {t.scope === "global" ? " · global (config.toml)" : ""}
        </span>
      </div>
      {own && !t.secret_ready && (
        <div className="kv"><span>Not sending</span>
          <b className="st-crit">
            {t.kind === "sheet"
              ? `${t.creds_env || "GOOGLE_SHEETS_CREDENTIALS"} is not set in .env on the server`
              : `${t.secret_env} is not set in .env on the server`}
          </b></div>
      )}
      {own && t.kind === "sheet" && (
        <div className="kv"><span>Writing to</span>
          <b>
            {t.sheet_mode === "service_account" ? (
              <a href={t.url} target="_blank" rel="noreferrer">open the sheet</a>
            ) : (
              "the sheet's own Apps Script"
            )}
            {" · tab "}<code>{t.sheet_tab || "Sheet1"}</code>
            {" · date | link | text | media"}
          </b></div>
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
          {(t.kind === "telegram" || t.kind === "sheet") && (
            <button className="btn btn-ghost btn-sm" onClick={() => setBackfilling(true)}>
              Send past posts…
            </button>
          )}
          <span style={{ flex: 1 }} />
          <button className="btn btn-danger btn-sm"
                  onClick={async () => { await api.removeDeliveryTarget(t.target_id); reload(); }}>
            Delete
          </button>
        </div>
      )}
      {backfilling && <BackfillModal t={t} onClose={() => setBackfilling(false)} />}
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
          Add a target — a Watch-Tower webhook, a Telegram chat or a Google
          Sheet — and every post this project collects is sent there, seconds
          after collection.
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
        <AddTargetModal pid={pid} serviceAccount={data?.sheet_service_account}
                        onDone={reload} onClose={() => setAdding(false)} />
      )}
    </>
  );
}
