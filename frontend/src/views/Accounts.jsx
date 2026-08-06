// Global: the scraper accounts and their session health, X and Instagram.
// Adding an account stays in the classic dashboard (it drives a streamed
// browser sign-in); this view links there rather than half-reimplementing it.
import React from "react";
import { api, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

const dotFor = (a) =>
  a.active && a.status !== "at_risk" ? "" : a.active ? " warn" : " bad";

export default function Accounts({ onMenu }) {
  const x = useApi(() => api.status(), [], { every: 30_000 });
  const ig = useApi(() => api.igStatus(), []);

  return (
    <>
      <PageHead title="Accounts & Sessions" onMenu={onMenu}
                sub="Global — one pool of accounts serves every project">
        <a className="btn btn-brand" href="/accounts">+ Add / sign in (classic)</a>
      </PageHead>

      <div className="feed-head"><h2>X</h2></div>
      {x.loading && !x.data && <Loading />}
      {x.error && !x.data && <ErrorState error={x.error} retry={x.reload} />}
      {x.data && (x.data.accounts || []).length === 0 && (
        <Empty title="No X accounts yet">
          Use “Add / sign in” — a browser window opens for the one-time login.
        </Empty>
      )}
      {(x.data?.accounts || []).map((a) => (
        <div className="panel" key={a.label || a.username}>
          <div className="phead">
            <h3>
              <span className={`dot${dotFor(a)}`} style={{ display: "inline-block", marginRight: 9 }} />
              @{a.username || a.label}
            </h3>
            <span className="right">{a.proxy ? "proxied" : "no proxy"} · {a.requests ?? 0} requests</span>
          </div>
          {(a.reasons || []).map((r, i) => (
            <div className="kv" key={i}><span>note</span><b>{r}</b></div>
          ))}
          {a.error && <div className="kv"><span>error</span><b className="st-crit">{a.error}</b></div>}
          {a.action && <div className="kv"><span>do</span><b>{a.action}</b></div>}
        </div>
      ))}

      <div className="feed-head" style={{ marginTop: 18 }}><h2>Instagram</h2></div>
      {ig.loading && !ig.data && <Loading />}
      {ig.data && (ig.data.accounts || []).length === 0 && (
        <Empty title="No Instagram accounts yet">
          Onboard with <code>ig_login.py</code> or a session cookie via <code>ig_import.py</code>.
        </Empty>
      )}
      {(ig.data?.accounts || []).map((a) => (
        <div className="panel" key={a.username}>
          <div className="phead">
            <h3>
              <span className={`dot${a.active ? "" : " bad"}`} style={{ display: "inline-block", marginRight: 9 }} />
              {a.username}
            </h3>
            <span className="right">{a.proxy ? "proxied" : "no proxy"}</span>
          </div>
          {a.error && <div className="kv"><span>error</span><b className="st-crit">{a.error}</b></div>}
        </div>
      ))}
    </>
  );
}
