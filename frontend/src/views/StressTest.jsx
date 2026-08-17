// Stress Test — deliberately push ONE account with back-to-back requests to
// see how many posts it pulls before it goes "hot" (rate-limited / challenged),
// with a per-request latency (hotness) graph.
//
// Extensible by design: the platform list and per-platform accounts come from
// the server (/api/stress/accounts). Add a platform in stress.py and it shows
// up here automatically — no change to this file.
import React, { useEffect, useMemo, useState } from "react";
import { api, useApi, fmtN, fmtLag } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Loading, ErrorState } from "../components/ui.jsx";

const LABEL = { x: "X / Twitter", ig: "Instagram", fb: "Facebook" };
const platName = (p) => LABEL[p] || p.toUpperCase();

// A small dependency-free line chart of per-request latency. Hot requests are
// drawn red; the point where the account went hot gets a marker. This is the
// "hotness rises as we keep fetching" picture the user asked for.
function HotnessChart({ steps }) {
  const W = 640, H = 220, PL = 44, PR = 12, PT = 16, PB = 28;
  const iw = W - PL - PR, ih = H - PT - PB;
  if (!steps || steps.length === 0) return null;
  const lat = steps.map((s) => s.latency_ms || 0);
  const maxLat = Math.max(100, ...lat);
  const n = steps.length;
  const x = (i) => PL + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => PT + ih - (v / maxLat) * ih;

  const pts = steps.map((s, i) => [x(i), y(s.latency_ms || 0)]);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const ticks = [0, 0.5, 1].map((f) => Math.round(maxLat * f));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
         aria-label="Per-request latency" style={{ maxWidth: 680 }}>
      {ticks.map((t, i) => {
        const yy = y(t);
        return (
          <g key={i}>
            <line x1={PL} y1={yy} x2={W - PR} y2={yy}
                  stroke="#e5e7eb" strokeWidth="1" />
            <text x={PL - 6} y={yy + 4} textAnchor="end"
                  fontSize="10" fill="#9ca3af">{fmtLag(t)}</text>
          </g>
        );
      })}
      <path d={path} fill="none" stroke="#6366f1" strokeWidth="2" />
      {steps.map((s, i) => (
        <circle key={i} cx={x(i)} cy={y(s.latency_ms || 0)} r={s.hot ? 6 : 3.5}
                fill={s.hot ? "#ef4444" : (s.posts ? "#6366f1" : "#f59e0b")}>
          <title>{`#${s.i} · ${fmtLag(s.latency_ms)} · ${s.posts} posts${s.hot ? " · HOT" : ""}\n${s.note || ""}`}</title>
        </circle>
      ))}
      <text x={PL} y={H - 8} fontSize="10" fill="#9ca3af">request #1</text>
      <text x={W - PR} y={H - 8} fontSize="10" fill="#9ca3af"
            textAnchor="end">#{n}</text>
    </svg>
  );
}

export default function StressTest({ onMenu }) {
  const { data: meta, error: metaErr, loading: metaLoading } =
    useApi(() => api.stressAccounts(), []);
  const platforms = meta?.platforms || [];

  const [platform, setPlatform] = useState("");
  const [account, setAccount] = useState("");
  const [target, setTarget] = useState("");
  const [n, setN] = useState(20);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [res, setRes] = useState(null);
  const [pending, setPending] = useState(null); // guard payload awaiting ack

  useEffect(() => {
    if (!platform && platforms.length) setPlatform(platforms[0]);
  }, [platforms, platform]);
  useEffect(() => { setAccount(""); }, [platform]);

  const accounts = meta?.accounts?.[platform] || [];

  const doRun = async (ack) => {
    setBusy(true); setErr(""); if (!ack) setRes(null);
    try {
      const out = await api.stressRun({
        platform, target: target.trim(), n: Number(n),
        account: account || undefined, ack: !!ack,
      });
      if (out.needs_ack) { setPending(out); setBusy(false); return; }
      setPending(null);
      setRes(out);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const canRun = platform && target.trim() && !busy;
  const hotAt = res?.hot_at;

  return (
    <>
      <PageHead title="Stress Test" onMenu={onMenu}
                sub="Find how many posts an account pulls before it goes hot">
      </PageHead>

      <div className="panel" style={{ borderLeft: "3px solid #ef4444" }}>
        <div className="phead"><h3><span className="st-crit">⛔</span> Throwaway accounts only</h3></div>
        <div className="kv"><span>why</span><b>
          This is the one tool that is meant to rate-limit an account. It fires
          back-to-back requests until the account is throttled or challenged.
          Never point it at an account you need.
        </b></div>
      </div>

      {metaLoading && !meta && <Loading label="Loading accounts…" />}
      {metaErr && !meta && <ErrorState error={metaErr} />}

      {meta && (
        <div className="panel">
          <div className="phead"><h3>Run a stress pass</h3></div>

          <div className="field">
            <label>Platform</label>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              {platforms.map((p) => (
                <button key={p}
                        className={`btn ${platform === p ? "btn-brand" : "btn-ghost"}`}
                        onClick={() => setPlatform(p)}>
                  {platName(p)}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label htmlFor="st-target">
              {platform === "x"
                ? "Target — @handle or an advanced-search query"
                : "Target — @handle or numeric id"}
            </label>
            <input id="st-target" value={target} autoComplete="off"
                   onChange={(e) => setTarget(e.target.value)}
                   placeholder={platform === "x" ? "nasa   ·   from:nasa min_faves:100"
                                                  : "natgeo   ·   787132"} />
          </div>

          {accounts.length > 0 && (
            <div className="field">
              <label htmlFor="st-acct">
                Fetch with {platform === "x" ? "(X serves from the pool)" : "(defaults to the active account)"}
              </label>
              <select id="st-acct" value={account}
                      onChange={(e) => setAccount(e.target.value)}>
                <option value="">active / default</option>
                {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </div>
          )}

          <div className="field">
            <label htmlFor="st-n">Requests to fire: <b>{n}</b></label>
            <input id="st-n" type="range" min="1" max="100" value={n}
                   onChange={(e) => setN(e.target.value)} />
          </div>

          {err && <div className="err">{err}</div>}
          <div className="row">
            <button className="btn btn-brand" disabled={!canRun}
                    onClick={() => doRun(false)}>
              {busy ? "Running…" : "Run stress test"}
            </button>
          </div>
        </div>
      )}

      {pending && (
        <div className="panel" style={{ borderLeft: "3px solid #f59e0b" }}>
          <div className="phead"><h3><span className="st-warn">⚠</span> Confirm — this will heat the account</h3></div>
          <div className="kv"><span>warning</span><b>{pending.warning}</b></div>
          {(pending.notes || []).length > 0 && (
            <div className="kv"><span>guard</span><b>{pending.notes.join("; ")}</b></div>
          )}
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setPending(null)}>Cancel</button>
            <button className="btn btn-brand" disabled={busy}
                    onClick={() => doRun(true)}>
              {busy ? "Running…" : "I understand — run it"}
            </button>
          </div>
        </div>
      )}

      {res && !res.error && (
        <div className="panel">
          <div className="phead">
            <h3>Result — {platName(res.platform)} · {res.target}</h3>
            <span className="right">{res.account}</span>
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 18, marginBottom: 8 }}>
            <div><small>requests fired</small><div style={{ fontSize: 22, fontWeight: 700 }}>{fmtN(res.requests)}</div></div>
            <div><small>posts pulled</small><div style={{ fontSize: 22, fontWeight: 700 }}>{fmtN(res.cum_posts)}</div></div>
            <div>
              <small>went hot at</small>
              <div style={{ fontSize: 22, fontWeight: 700, color: hotAt ? "#ef4444" : "#16a34a" }}>
                {hotAt ? `#${hotAt}` : "stayed cold"}
              </div>
            </div>
          </div>

          <HotnessChart steps={res.steps} />

          <div style={{ overflowX: "auto", marginTop: 10 }}>
            <table className="tbl" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "#6b7280" }}>
                  <th>#</th><th>latency</th><th>posts</th><th>cumulative</th><th>note</th>
                </tr>
              </thead>
              <tbody>
                {res.steps.map((s) => (
                  <tr key={s.i} style={{ background: s.hot ? "#fef2f2" : undefined }}>
                    <td>{s.i}</td>
                    <td>{fmtLag(s.latency_ms)}</td>
                    <td>{s.posts}</td>
                    <td>{fmtN(s.cum_posts)}</td>
                    <td style={{ color: s.hot ? "#b91c1c" : "#6b7280" }}>{s.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {(res.log || []).length > 0 && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: "pointer", color: "#6b7280" }}>engine log</summary>
              <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "#6b7280" }}>
                {res.log.join("\n")}
              </pre>
            </details>
          )}
        </div>
      )}

      {res && res.error && (
        <div className="panel" style={{ borderLeft: "3px solid #ef4444" }}>
          <div className="phead"><h3><span className="st-crit">⛔</span> Run failed</h3></div>
          <div className="kv"><span>error</span><b>{res.error}</b></div>
          {(res.log || []).length > 0 && (
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "#6b7280" }}>
              {res.log.join("\n")}
            </pre>
          )}
        </div>
      )}
    </>
  );
}
