// Guard findings — advisory only, exactly as guard.py works: it warns,
// it never changes anything.
import React from "react";
import { api, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

// guard.py levels: "block" | "warn" | "info" (Finding.level)
const SEV = {
  block: ["⛔", "st-crit"],
  warn: ["⚠", "st-warn"],
  info: ["ℹ", ""],
};

export default function Guard({ onMenu }) {
  const { data, error, loading, reload } = useApi(() => api.guard(), []);
  const all = data?.findings || [];

  return (
    <>
      <PageHead title="Guard" onMenu={onMenu}
                sub="Advisory risk checks — it warns, it never changes anything">
        <button className="btn btn-ghost" onClick={() => reload()}>Re-check</button>
      </PageHead>

      {loading && !data && <Loading label="Assessing…" />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && all.length === 0 && (
        <Empty title="Nothing to worry about">
          No risk findings right now. Guard checks budget, shared IPs, dead
          accounts, stale sessions and exposure.
        </Empty>
      )}
      {all.map((f, i) => {
        const [icon, cls] = SEV[f.level] || SEV.info;
        return (
          <div className="panel" key={i}>
            <div className="phead">
              <h3><span className={cls}>{icon}</span> {f.title || "finding"}</h3>
              <span className="right">{f.code}</span>
            </div>
            {f.detail && <div className="kv"><span>detail</span><b>{f.detail}</b></div>}
            {f.remedy && <div className="kv"><span>fix</span><b>{f.remedy}</b></div>}
          </div>
        );
      })}
    </>
  );
}
