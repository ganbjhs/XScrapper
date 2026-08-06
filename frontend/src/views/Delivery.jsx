// The pipe's outbound half: every delivery target, its cursor, and its
// failures. "Behind: 0" here is the whole point of the product.
import React from "react";
import { api, fmtAgo, fmtN, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

export default function Delivery({ onMenu }) {
  const { data, error, loading, reload } = useApi(() => api.delivery(), [], { every: 10_000 });
  const targets = data?.targets || [];

  return (
    <>
      <PageHead title="Delivery" onMenu={onMenu}
                sub="Where every collected post goes, and how far each receiver has got">
        <button className="btn btn-ghost" onClick={() => reload()}>Refresh</button>
      </PageHead>

      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && targets.length === 0 && (
        <Empty title="No delivery targets yet">
          Declare a <code>[[webhooks]]</code> block in config.toml for Watch-Tower,
          or switch Telegram on for a stream in the classic dashboard.
        </Empty>
      )}

      {targets.map((t) => (
        <div className="panel" key={t.label}>
          <div className="phead">
            <h3>
              <span className={`dot${t.failures ? " bad" : t.behind ? " warn" : ""}`}
                    style={{ display: "inline-block", marginRight: 9 }} />
              {t.label}
            </h3>
            <span className="right">{t.kind} → {t.url}</span>
          </div>
          {!t.started ? (
            <div className="kv">
              <span>Waiting for its first delivery</span>
              <b>starts from now, not the archive</b>
            </div>
          ) : (
            <>
              <div className="kv">
                <span>Cursor behind</span>
                <b className={t.behind ? "st-warn" : "st-good"}>
                  {t.behind ? `${fmtN(t.behind)} posts` : "✓ 0 — in sync"}
                </b>
              </div>
              <div className="kv"><span>Delivered (lifetime)</span><b>{fmtN(t.sent)}</b></div>
              <div className="kv"><span>Last success</span>
                <b>{t.last_ok_ms ? fmtAgo(t.last_ok_ms) : "never"}</b></div>
              <div className="kv"><span>Consecutive failures</span>
                <b className={t.failures ? "st-crit" : ""}>{fmtN(t.failures)}</b></div>
              {t.last_error && (
                <div className="kv"><span>Last error</span>
                  <b className="st-crit">{t.last_error}</b></div>
              )}
              {t.streams?.length > 0 && (
                <div className="kv"><span>Scoped to streams</span><b>{t.streams.join(", ")}</b></div>
              )}
            </>
          )}
        </div>
      ))}

      {targets.length > 0 && (
        <div className="state" style={{ textAlign: "left" }}>
          <b>How delivery works</b>
          Position is a cursor in the database, not a queue — a receiver that goes
          down catches up by itself when it returns, and nothing is ever lost.
          Payloads are HMAC-signed and include full media (photos, video files and
          thumbnails). Receivers de-duplicate on <code>tweet_id</code>.
        </div>
      )}
    </>
  );
}
