// Collector → Watch-Tower: what this project handed over, and what is waiting.
//
// Watch-Tower does not receive a push from us; it PULLS with an API key and a
// cursor. The old panel here described webhooks ("Not set up — declare a
// [[webhooks]] target"), which nobody uses, so the one question asked every
// time a feed over there looks thin — "did the collector send it?" — had no
// answer on this screen. This is that answer, for the project in view, from
// the server's own ledger of every pull (/api/handover).
import React, { useState } from "react";
import { fmtAgo, fmtLag, fmtN } from "../api/client.js";

const NAME = { x: "X", instagram: "Instagram" };
const ORDER = ["x", "instagram"];

const clock = (ms) => (ms ? new Date(ms).toLocaleTimeString("en-IN",
  { hour: "2-digit", minute: "2-digit", hour12: false }) : "—");
const stamp = (ms) => (ms ? new Date(ms).toLocaleString("en-IN",
  { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—");

// One line a person can say out loud, per state.
function verdict(p) {
  if (!p || p.state === "error") return { cls: "st-warn", head: "Unknown", line: p?.error || "could not read the ledger" };
  if (p.state === "never") {
    return { cls: "", head: "No pull yet",
             line: "Watch-Tower has not pulled this platform since recording began" };
  }
  if (p.state === "stalled") {
    return { cls: "st-crit", head: "⚠ Stalled",
             line: `Watch-Tower has not pulled for ${fmtAgo(p.last_ok_ms).replace(" ago", "")}` };
  }
  if (p.state === "behind") {
    return { cls: "st-warn", head: `⚠ ${fmtN(p.waiting)} waiting`,
             line: `oldest has waited ${fmtAgo(p.waiting_since_ms).replace(" ago", "")}` };
  }
  if (p.state === "flowing") {
    return { cls: "st-good", head: `${fmtN(p.waiting)} waiting`,
             line: `the next pull takes them · last pull ${fmtAgo(p.last_ok_ms)}` };
  }
  return { cls: "st-good", head: "✓ In sync", line: `last pull ${fmtAgo(p.last_ok_ms)}` };
}

const pick = (data, source) => {
  const all = data?.platforms || {};
  const keys = ORDER.filter((k) => all[k] && (source === "all" || source === k));
  return { all, keys };
};

// Worst state wins when several platforms are in view.
const RANK = { stalled: 5, behind: 4, error: 3, flowing: 2, in_sync: 1, never: 0 };

export function HandoverStat({ data, source }) {
  const { all, keys } = pick(data, source);
  if (source === "facebook") {
    return (
      <div className="stat">
        <div className="k">To Watch-Tower</div>
        <div className="v" style={{ fontSize: 18 }}>—</div>
        <div className="d">Facebook is not pulled by cursor</div>
      </div>
    );
  }
  const live = keys.filter((k) => all[k].state !== "never");
  const worst = [...(live.length ? live : keys)].sort((a, b) => RANK[all[b].state] - RANK[all[a].state])[0];
  const p = all[worst];
  const v = verdict(p);
  const taken = live.reduce((n, k) => n + (all[k].taken_today || 0), 0);
  const collected = live.reduce((n, k) => n + (all[k].collected_today || 0), 0);
  return (
    <div className="stat">
      <div className="k">To Watch-Tower{source !== "all" && NAME[source] ? ` · ${NAME[source]}` : ""}</div>
      {!data ? <div className="v" style={{ fontSize: 18 }}>—</div> : (
        <>
          <div className={`v ${v.cls}`} style={{ fontSize: 20 }}>{v.head}</div>
          <div className="d">
            {live.length > 0
              ? `taken ${fmtN(taken)} of ${fmtN(collected)} today · ` +
                (p.state === "in_sync" || p.state === "flowing"
                  ? `last pull ${fmtAgo(p.last_ok_ms)}` : v.line)
              : v.line}
          </div>
        </>
      )}
    </div>
  );
}

function proofText(data, projectName, keys) {
  const L = [];
  L.push(`Collector → Watch-Tower · ${projectName || `project ${data.project_id}`}`);
  L.push(`As of ${stamp(data.now_ms)} IST (today = since ${stamp(data.day_start_ms)} IST)`);
  for (const k of keys) {
    const p = data.platforms[k];
    L.push("");
    L.push(`${NAME[k]}`);
    if (p.state === "error") { L.push(`  could not be read: ${p.error}`); continue; }
    L.push(`  Collected today: ${fmtN(p.collected_today)}` +
           (p.newest_collected_ms ? ` (newest at ${clock(p.newest_collected_ms)})` : ""));
    if (p.state === "never") {
      L.push("  Watch-Tower has not pulled this platform for this project since recording began.");
      continue;
    }
    L.push(`  Taken by Watch-Tower today: ${fmtN(p.taken_today)}` +
           ` (${fmtN(p.confirmed_today)} confirmed by its next request)`);
    L.push(`  Waiting for its next pull: ${fmtN(p.waiting)}` +
           (p.waiting ? ` (oldest collected at ${clock(p.waiting_since_ms)})` : ""));
    L.push(`  Watch-Tower holds everything collected up to ${stamp(p.served_ms ?? p.ack_ms)}`);
    L.push(`  Pulls today: ${fmtN(p.pulls_today)} (${fmtN(p.errors_today)} failed, ` +
           `${fmtN(p.empty_today)} found nothing new) · last ${stamp(p.last_ok_ms)}` +
           (p.typical_gap_ms ? ` · about every ${fmtLag(p.typical_gap_ms)}` : ""));
    if (p.avg_wait_ms != null) {
      L.push(`  A post waits ${fmtLag(p.avg_wait_ms)} on average before Watch-Tower takes it` +
             (p.max_wait_ms ? ` (longest ${fmtLag(p.max_wait_ms)})` : ""));
    }
  }
  L.push("");
  L.push("Counted from the collector's database against the cursor Watch-Tower itself sends. " +
         "What Watch-Tower does with a post after taking it (keyword and relevance filters, " +
         "its push into the project feed) is not visible from the collector.");
  return L.join("\n");
}

export default function HandoverPanel({ data, loading, error, source, projectName }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const { all, keys } = pick(data, source === "facebook" ? "none" : source);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(proofText(data, projectName, keys));
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch { /* clipboard refused: the numbers are on screen anyway */ }
  };
  return (
    <div className="panel">
      <div className="phead">
        <h3>Collector → Watch-Tower</h3>
        <span className="right">this project · pulled by API key</span>
      </div>
      {loading && !data && <div className="sub" style={{ color: "var(--ink-3)" }}>Loading…</div>}
      {error && !data && <div className="sub st-crit">{error}</div>}
      {data && keys.length === 0 && (
        <div className="kv"><span>Facebook</span><b>not pulled by cursor — no hand-over record</b></div>
      )}
      {data && keys.map((k) => {
        const p = all[k];
        const v = verdict(p);
        const none = p.state === "never" || p.state === "error";
        return (
          <div className="ho-sec" key={k}>
            <div className="ho-h">
              <b>{NAME[k]}</b>
              <span className={v.cls}>{v.head}</span>
            </div>
            <div className="ho-flow">
              <div><span>Collected today</span><b>{fmtN(p.collected_today)}</b></div>
              <i>→</i>
              <div><span>Taken by WT</span><b className={none ? "" : "st-good"}>{none ? "—" : fmtN(p.taken_today)}</b></div>
              <i>·</i>
              <div><span>Waiting</span>
                <b className={p.state === "behind" || p.state === "stalled" ? "st-crit" : ""}>
                  {none ? "—" : fmtN(p.waiting)}</b></div>
            </div>
            <div className="ho-line">{v.line}</div>
            {!none && (
              <>
                <div className="kv"><span>WT holds everything up to</span><b>{stamp(p.served_ms ?? p.ack_ms)}</b></div>
                <div className="kv"><span>Pulls today</span>
                  <b>{fmtN(p.pulls_today)}
                    {p.errors_today > 0 && <span className="st-crit"> · {fmtN(p.errors_today)} failed</span>}
                    {p.typical_gap_ms ? ` · every ~${fmtLag(p.typical_gap_ms)}` : ""}</b></div>
                {p.avg_wait_ms != null && (
                  <div className="kv"><span>Wait before WT takes a post</span>
                    <b>{fmtLag(p.avg_wait_ms)} avg{p.max_wait_ms ? ` · ${fmtLag(p.max_wait_ms)} max` : ""}</b></div>
                )}
              </>
            )}
            {open && (p.recent || []).length > 0 && (
              <table className="ho-pulls">
                <thead><tr><th>Pull at</th><th>Rows</th><th>Up to</th><th>Status</th></tr></thead>
                <tbody>
                  {p.recent.map((r) => (
                    <tr key={r.at_ms}>
                      <td>{clock(r.at_ms)}</td>
                      <td>{fmtN(r.rows)}</td>
                      <td>{r.to_ms ? clock(r.to_ms) : "—"}</td>
                      <td className={r.status >= 300 ? "st-crit" : ""}>{r.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
      {data && keys.length > 0 && (
        <>
          <div className="ho-foot">
            After a post is taken, Watch-Tower applies its own keyword and
            relevance filters before it reaches a project feed. That part is
            not visible from here.
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <button className="btn btn-ghost btn-sm" onClick={copy}>
              {copied ? "Copied ✓" : "Copy as proof"}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => setOpen((o) => !o)}>
              {open ? "Hide pulls" : "Show last pulls"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
