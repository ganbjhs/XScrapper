// The activity view, two lenses on "what is the collector doing":
//   * Polls — the structured X poll history (what it fetched, what it cost).
//   * Account log — the RAW line-by-line log the FB/IG collectors and engines
//     write while acting as the burner accounts: session reuse, login
//     attempts, logged-out walls, fetches, avatar captures, errors. This is
//     the eye-on-the-accounts view: if a login is failing, it says so here in
//     the engine's own words.
import React, { useState } from "react";
import { api, fmtAgo, fmtLag, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

const STOP = {
  watermark: ["✓ caught up", "st-good"],
  exhausted: ["exhausted", ""],
  page_budget: ["page budget", "st-warn"],
  no_account_or_abort: ["no account!", "st-crit"],
  error: ["error", "st-crit"],
};

const LEVEL_CLS = { info: "", warn: "st-warn", error: "st-crit" };
const PLATFORMS = [
  ["", "All"],
  ["facebook", "Facebook"],
  ["instagram", "Instagram"],
  ["x", "X"],
];

function fmtWhen(ms) {
  if (!ms) return "—";
  const d = new Date(ms);
  const today = new Date().toDateString() === d.toDateString();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return today ? hm : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}

function AccountLog() {
  const [platform, setPlatform] = useState("");
  const [onlyProblems, setOnlyProblems] = useState(false);
  const { data, error, loading, reload } = useApi(
    () => api.activityLogs({ limit: 300, platform }), [platform], { every: 10_000 });
  let events = data?.events || [];
  if (onlyProblems) events = events.filter((e) => e.level !== "info");

  return (
    <>
      <div className="row" style={{ gap: 8, margin: "10px 0", flexWrap: "wrap", alignItems: "center" }}>
        {PLATFORMS.map(([v, label]) => (
          <button key={v || "all"}
                  className={`btn ${platform === v ? "btn-brand" : "btn-ghost"}`}
                  onClick={() => setPlatform(v)}>
            {label}
          </button>
        ))}
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginLeft: 8 }}>
          <input type="checkbox" checked={onlyProblems}
                 onChange={(e) => setOnlyProblems(e.target.checked)} />
          problems only
        </label>
        <div className="grow" />
        <button className="btn btn-ghost" onClick={() => reload()}>Refresh</button>
      </div>

      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && events.length === 0 && (
        <Empty title="No account activity recorded yet">
          Lines appear here as soon as a collector runs — a dashboard
          “Fetch now”, or the <code>collect_fb.py</code> / <code>collect_ig.py</code> services.
          Every login attempt, fetch and error the accounts produce is kept.
        </Empty>
      )}

      {events.length > 0 && (
        <div className="panel" style={{ padding: "6px 12px", overflowX: "auto" }}>
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ whiteSpace: "nowrap" }}>When</th>
                <th>Platform</th><th>Account</th><th>Activity</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td style={{ whiteSpace: "nowrap" }} title={new Date(e.ts_ms).toLocaleString()}>
                    {fmtWhen(e.ts_ms)}
                  </td>
                  <td>
                    <span className={`badge platform-${{ facebook: "fb", instagram: "ig" }[e.platform] || "x"}`}>
                      {{ facebook: "f", instagram: "IG" }[e.platform] || "𝕏"}
                    </span>
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>{e.account || "—"}</td>
                  <td className={LEVEL_CLS[e.level] || ""}
                      style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                               fontSize: 12, overflowWrap: "anywhere" }}>
                    {e.message}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function PollLog() {
  const { data, error, loading, reload } = useApi(
    () => api.activity({ limit: 120 }), [], { every: 15_000 });
  const polls = data?.polls || [];

  return (
    <>
      <div className="row" style={{ margin: "10px 0" }}>
        <div className="grow" />
        <button className="btn btn-ghost" onClick={() => reload()}>Refresh</button>
      </div>
      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && polls.length === 0 && (
        <Empty title="No polls recorded yet">
          The log fills as soon as the collector runs: <code>python3 main.py watch --all</code>
        </Empty>
      )}

      {polls.length > 0 && (
        <div className="panel" style={{ padding: "6px 12px", overflowX: "auto" }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>When</th><th>Stream</th><th>Kind</th><th>Account</th>
                <th style={{ textAlign: "right" }}>Pages</th>
                <th style={{ textAlign: "right" }}>New</th>
                <th style={{ textAlign: "right" }}>Lag p50</th>
                <th>Stopped because</th>
              </tr>
            </thead>
            <tbody>
              {polls.map((p) => {
                const [label, cls] = STOP[p.stop_reason] || [p.stop_reason || "—", ""];
                return (
                  <tr key={p.poll_id}>
                    <td style={{ whiteSpace: "nowrap" }}>{fmtAgo(p.started_ms)}</td>
                    <td style={{ overflowWrap: "anywhere" }}>{p.label}</td>
                    <td>{p.kind}</td>
                    <td>{p.account ? `@${p.account}` : "—"}</td>
                    <td className="num">{p.pages}</td>
                    <td className="num">{p.new_tweets}</td>
                    <td className="num">{p.lag_p50_ms != null ? fmtLag(p.lag_p50_ms) : "—"}</td>
                    <td className={cls}>{label}{p.error ? ` — ${p.error}` : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function Activity({ onMenu }) {
  const [tab, setTab] = useState("accounts");
  return (
    <>
      <PageHead title="Activity Log" onMenu={onMenu}
                sub="Account log: every login, fetch and error, straight from the collectors · Polls: the X poll history">
        <button className={`btn ${tab === "accounts" ? "btn-brand" : "btn-ghost"}`}
                onClick={() => setTab("accounts")}>
          Account log
        </button>
        <button className={`btn ${tab === "polls" ? "btn-brand" : "btn-ghost"}`}
                onClick={() => setTab("polls")}>
          Polls
        </button>
      </PageHead>
      {tab === "accounts" ? <AccountLog /> : <PollLog />}
    </>
  );
}
