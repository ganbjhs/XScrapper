// The poll log — what the collector actually did, poll by poll.
import React from "react";
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

export default function Activity({ onMenu }) {
  const { data, error, loading, reload } = useApi(
    () => api.activity({ limit: 120 }), [], { every: 15_000 });
  const polls = data?.polls || [];

  return (
    <>
      <PageHead title="Activity Log" onMenu={onMenu}
                sub="Every poll: what it fetched, what it cost, why it stopped">
        <button className="btn btn-ghost" onClick={() => reload()}>Refresh</button>
      </PageHead>

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
