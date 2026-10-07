// Diagnosis — what the Instagram accounts' failures have in common.
//
// Built from the sign-in evidence the server now keeps for every attempt
// (ig_evidence.py, 2026-10-07): the status Instagram answered with, who
// answered, the address, the browser that was really launched. The server
// compares those ACROSS accounts and says what is shared; this panel prints
// its words and the evidence under them. "Run probe" is a measurement, not an
// inference: the login page asked by four different clients through one
// account's proxy.
import React, { useEffect, useState } from "react";
import { api, useApi } from "../api/client.js";

export default function Diagnosis({ accounts, compact }) {
  const [busy, setBusy] = useState(false);
  const d = useApi(() => api.igDiagnosis(), [], { every: busy ? 3000 : 30000 });
  const [pick, setPick] = useState("");
  const [err, setErr] = useState("");
  const [open, setOpen] = useState({});
  const data = d.data;
  const probe = data?.probe;
  useEffect(() => { if (probe) setBusy(!!probe.running); }, [probe?.running]);
  if (!data) return null;
  const igs = (accounts || []).filter((a) => a.platform === "ig");
  const findings = data.findings || [];
  const run = async () => {
    setErr("");
    try {
      const r = await api.igProbe(Number(pick || igs[0]?.account_id));
      if (r.error) { setErr(r.error); return; }
      setBusy(true); d.reload && d.reload();
    } catch (e) { setErr(String(e.message || e)); }
  };
  if (compact && findings.length === 0) return null;
  const CHIP = { measured: ["measured", "good"], shared: ["shared", "crit"], single: ["one account", "warn"] };
  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="phead">
        <h3>Diagnosis</h3>
        <span className="right">Instagram · last {data.window_hours}h</span>
      </div>
      {findings.length === 0 && <div className="kv"><span>shared problems</span><b className="st-good">none</b></div>}
      {findings.map((f, i) => (
        <div key={i} style={{ borderTop: "1px solid var(--ring)", padding: "6px 0" }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer", fontSize: 12.5 }}
               onClick={() => setOpen((o) => ({ ...o, [i]: !o[i] }))}>
            <span className={`chip ${(CHIP[f.level] || CHIP.single)[1]}`}>{(CHIP[f.level] || CHIP.single)[0]}</span>
            <b style={{ flex: 1, fontWeight: 600 }}>{f.title}</b>
            <span style={{ color: "var(--ink-3)" }}>{open[i] ? "▾" : "›"}</span>
          </div>
          {open[i] && (
            <div style={{ fontSize: 12.5, lineHeight: 1.6, margin: "6px 0 4px", color: "var(--ink-2)" }}>
              {f.detail}
              {(f.evidence || []).length > 0 && (
                <ul style={{ margin: "6px 0 0 18px", fontFamily: "var(--mono, ui-monospace, monospace)", fontSize: 12 }}>
                  {f.evidence.map((e, j) => <li key={j}>{e}</li>)}
                </ul>
              )}
            </div>
          )}
        </div>
      ))}
      {!compact && igs.length > 0 && (
        <div style={{ borderTop: "1px solid var(--ring)", paddingTop: 9, marginTop: 2 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <select value={pick || igs[0]?.account_id} onChange={(e) => setPick(e.target.value)} disabled={busy}>
              {igs.map((a) => <option key={a.account_id} value={a.account_id}>{a.label}</option>)}
            </select>
            <button className="btn btn-sm" onClick={run} disabled={busy}
                    title="Asks for Instagram's login page through this account's proxy as four clients (curl, HTTP with the phone's headers, the browser as the phone, the browser as itself). No cookies, no account. About a minute.">
              {busy ? "Probing…" : "Run probe"}
            </button>
            {probe?.result && !busy && (
              <button className="btn btn-ghost btn-sm" onClick={() => setOpen((o) => ({ ...o, probe: !o.probe }))}>
                {open.probe ? "Hide result" : "Last result"}
              </button>
            )}
            {err && <span className="st-crit" style={{ fontSize: 12.5 }}>{err}</span>}
          </div>
          {probe && (busy || open.probe) && (probe.lines || []).length > 0 && (
            <pre style={{ marginTop: 8, fontSize: 12, lineHeight: 1.6, whiteSpace: "pre-wrap",
                          border: "1px solid var(--ring)", borderRadius: 8, padding: "8px 10px" }}>
              {probe.lines.join("\n")}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
