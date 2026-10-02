// The activity view, two lenses on "what is the collector doing":
import React, { useMemo, useRef, useState } from "react";
import { api, fmtAgo, fmtLag, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, HeadSearch, Loading, Pill } from "../components/ui.jsx";

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
  // Classify runs log here too: what they sent, what they cost, why they
  ["classify", "Labelling"],
];

function fmtWhen(ms) {
  if (!ms) return "—";
  const d = new Date(ms);
  const today = new Date().toDateString() === d.toDateString();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return today ? hm : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}

// ---------------------------------------------------------------------------
// The timeline: one line per event, to the second, newest first — collectors,
// sign-ins, API keys, operator actions, account health, watchdog alerts. All of
// it is one table in activity.db; the category rides in `platform`.
// ---------------------------------------------------------------------------
const CATS = {
  security: ["Security", "SEC", "cat-sec"],
  api: ["API keys", "API", "cat-api"],
  operator: ["Operator", "OPS", "cat-ops"],
  system: ["System", "SYS", "cat-sys"],
  x: ["X", "𝕏", "platform-x"],
  instagram: ["Instagram", "IG", "platform-ig"],
  facebook: ["Facebook", "f", "platform-fb"],
  classify: ["Labelling", "AI", "cat"],
};
const CAT_OPTS = [["", "Everything"], ["security", "Security"], ["api", "API keys"],
  ["operator", "Operator actions"], ["accounts", "Accounts & collectors (all)"],
  ["x", "— X"], ["instagram", "— Instagram"], ["facebook", "— Facebook"],
  ["system", "System / watchdog"], ["classify", "Labelling"]];
const LEVELS = [["", "All levels"], ["problems", "Warnings + errors"], ["error", "Errors only"]];
const COLLECTORS = new Set(["x", "instagram", "facebook"]);
const LIMIT = 1000;
const DAY = 24 * 3600 * 1000;

const hms = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour12: false });
const dayOf = (ms) => {
  const d = new Date(ms);
  if (d.toDateString() === new Date().toDateString()) return "Today";
  if (d.toDateString() === new Date(Date.now() - DAY).toDateString()) return "Yesterday";
  return d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
};

function Tile({ k, v, d, tone, on, onClick }) {
  return (
    <button className={"stat sec-tile" + (on ? " on" : "")} onClick={onClick}>
      <div className="k">{k}</div>
      <div className={"v" + (v > 0 && tone ? ` ${tone}` : "")}>{v}</div>
      <div className="d">{d}</div>
    </button>
  );
}

function Timeline({ q, setQ, live }) {
  const [cat, setCat] = useState("");
  const [level, setLevel] = useState("");
  const { data, error, loading, reload } = useApi(
    () => api.activityLogs({ limit: LIMIT }), [], { every: live ? 5_000 : 0 });
  const all = data?.events || [];

  // Lines that arrived since the last refresh flash once.
  const seen = useRef(null);
  const fresh = useMemo(() => {
    const top = all[0]?.id || 0;
    const prev = seen.current;
    seen.current = top;
    return prev == null ? top : prev;
  }, [all]);

  const now = Date.now();
  const oldest = all.length ? all[all.length - 1].ts_ms : now;
  // The log is read LIMIT lines at a time: say honestly how far back that goes.
  const windowStart = Math.max(now - DAY, all.length >= LIMIT ? oldest : 0);
  const span = windowStart > now - DAY + 60_000 ? `since ${fmtWhen(windowStart)}` : "last 24 hours";
  const recent = all.filter((e) => e.ts_ms >= windowStart);
  const count = (f) => recent.filter(f).length;
  const failed = count((e) => e.platform === "security" && /sign-in (FAILED|BLOCKED)/.test(e.message));
  const refused = count((e) => e.platform === "security" && /key .*(REFUSED|rejected|rate limit)/i.test(e.message));
  const acct = count((e) => COLLECTORS.has(e.platform) && e.level === "error");
  const ops = count((e) => e.platform === "operator");
  const sys = count((e) => e.platform === "system");

  // Who is behind the security warnings: the callers to look at first.
  const suspects = useMemo(() => {
    const by = new Map();
    for (const e of recent) {
      if (e.platform !== "security" || e.level === "info" || !e.account) continue;
      const r = by.get(e.account) || { who: e.account, n: 0, errors: 0, last: 0 };
      r.n += 1; if (e.level === "error") r.errors += 1; r.last = Math.max(r.last, e.ts_ms);
      by.set(e.account, r);
    }
    return [...by.values()].sort((a, b) => b.errors - a.errors || b.n - a.n).slice(0, 8);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all]);

  const needle = q.trim().toLowerCase();
  const events = all.filter((e) =>
    (cat === "" || (cat === "accounts" ? COLLECTORS.has(e.platform) : e.platform === cat))
    && (level === "" || (level === "problems" ? e.level !== "info" : e.level === "error"))
    && (!needle || `${e.message || ""}\n${e.account || ""}`.toLowerCase().includes(needle)));

  const pick = (c, l) => { setCat(cat === c && level === l ? "" : c); setLevel(cat === c && level === l ? "" : l); };
  let lastDay = null;

  return (
    <>
      <section className="stats sec-stats">
        <Tile k="Failed sign-ins" v={failed} tone="st-crit" d={span}
              on={cat === "security" && level === "problems"} onClick={() => pick("security", "problems")} />
        <Tile k="Keys refused" v={refused} tone="st-crit" d="invalid, out of scope or rate-limited"
              on={cat === "security" && level === "error"} onClick={() => pick("security", "error")} />
        <Tile k="Account errors" v={acct} tone="st-crit" d="logins, sessions, proxies, quarantines"
              on={cat === "accounts" && level === "error"} onClick={() => pick("accounts", "error")} />
        <Tile k="Operator actions" v={ops} d="changes made from the dashboard"
              on={cat === "operator" && level === ""} onClick={() => pick("operator", "")} />
        <Tile k="Watchdog alerts" v={sys} tone="st-warn" d="raised by the system"
              on={cat === "system" && level === ""} onClick={() => pick("system", "")} />
      </section>

      <div className="fbar">
        <Pill label="Show" value={cat} onChange={setCat} options={CAT_OPTS} />
        <Pill label="Level" value={level} onChange={setLevel} options={LEVELS} />
        {data && (
          <span className="fbar-note">
            {events.length === all.length ? `${all.length} lines` : `${events.length} of ${all.length} lines`}
            {all[0] ? ` · last event ${fmtAgo(all[0].ts_ms)}` : ""}
          </span>
        )}
        <span className="grow" />
        {(cat || level || needle) && (
          <button className="freset" onClick={() => { setCat(""); setLevel(""); setQ(""); }}>Clear</button>
        )}
        <button className="btn btn-ghost btn-sm" onClick={() => reload()}>Refresh</button>
      </div>

      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && all.length === 0 && (
        <Empty title="Nothing recorded yet">
          Lines appear as soon as anything happens: a sign-in, an API pull, a
          collector run, a change made from the dashboard.
        </Empty>
      )}
      <div className="cols log-cols">
        {/* The left column always exists, so the side panels stay on the right
            even when the filters match nothing. */}
        <div className="log-main">
        {data && all.length > 0 && events.length === 0 && (
          <Empty title="No line matches">They look through the latest {all.length} lines.</Empty>
        )}
        {events.length > 0 && (
          <div className="panel tl" role="log">
            {events.map((e) => {
              const [name, short, cls] = CATS[e.platform] || [e.platform || "?", "?", "cat"];
              const d = dayOf(e.ts_ms);
              const head = d !== lastDay ? <div className="tl-day" key={`d${e.id}`}>{d}</div> : null;
              lastDay = d;
              return (
                <React.Fragment key={e.id}>
                  {head}
                  <div className={`tl-row lv-${e.level || "info"}` + (e.id > fresh ? " new" : "")}>
                    <time title={new Date(e.ts_ms).toLocaleString("en-IN")}>{hms(e.ts_ms)}</time>
                    <span className={`badge ${cls}`} title={name}>{short}</span>
                    {e.account
                      ? <button className="tl-who" title="Show only this caller / account"
                                onClick={() => setQ(String(e.account))}>{e.account}</button>
                      : <span className="tl-who none">—</span>}
                    <span className="tl-msg">{e.message}</span>
                  </div>
                </React.Fragment>
              );
            })}
            {all.length >= LIMIT && (
              <div className="tl-end">Showing the latest {LIMIT} lines — older ones are kept on the server (20,000 in all).</div>
            )}
          </div>
        )}
        </div>

        {data && all.length > 0 && (
          <aside>
            <div className="panel">
              <div className="phead"><h3>Threat watch</h3><span className="right">{span}</span></div>
              {suspects.length === 0 && (
                <div className="kv"><span className="st-good">No failed sign-ins or refused keys</span><b /></div>
              )}
              {suspects.map((r) => (
                <button className="sus" key={r.who} onClick={() => { setQ(r.who); setCat("security"); setLevel(""); }}>
                  <span className={`dot ${r.errors ? "bad" : "warn"}`} />
                  <span className="sus-who">{r.who}</span>
                  <span className="sus-n">{r.n} event{r.n === 1 ? "" : "s"} · {fmtAgo(r.last)}</span>
                </button>
              ))}
              <div className="ho-foot">
                An IP is a browser or program without a valid sign-in or key; “…abcd” is the
                last four characters of an API key. Click one to see everything it did.
              </div>
            </div>
            <div className="panel">
              <div className="phead"><h3>What is recorded</h3></div>
              <div className="kv"><span>Security</span><b>sign-ins, lockouts, refused keys</b></div>
              <div className="kv"><span>API keys</span><b>each pull, folded per minute</b></div>
              <div className="kv"><span>Operator</span><b>every change, path + result</b></div>
              <div className="kv"><span>Accounts</span><b>logins, fetches, quarantines</b></div>
              <div className="kv"><span>System</span><b>watchdog alerts</b></div>
              <div className="ho-foot">Passwords, request bodies and full keys are never written.</div>
            </div>
          </aside>
        )}
      </div>
    </>
  );
}

const RESULTS = [["", "All"], ["problems", "Problems only"], ["new", "Found new posts"]];
const POLL_BAD = new Set(["no_account_or_abort", "error", "page_budget"]);

function PollLog({ q }) {
  const [result, setResult] = useState("");
  const [kind, setKind] = useState("");
  const { data, error, loading, reload } = useApi(
    () => api.activity({ limit: 120 }), [], { every: 15_000 });
  const all = data?.polls || [];
  const kinds = [["", "All"], ...[...new Set(all.map((p) => p.kind).filter(Boolean))].sort().map((k) => [k, k])];
  const needle = q.trim().toLowerCase();
  const polls = all.filter((p) =>
    (!kind || p.kind === kind)
    && (result === "" || (result === "new" ? p.new_tweets > 0 : (POLL_BAD.has(p.stop_reason) || !!p.error)))
    && (!needle || `${p.label || ""}\n${p.account || ""}\n${p.error || ""}`.toLowerCase().includes(needle)));
  const bad = all.filter((p) => p.stop_reason === "error" || p.stop_reason === "no_account_or_abort" || p.error).length;

  return (
    <>
      <div className="fbar">
        <Pill label="Result" value={result} onChange={setResult} options={RESULTS} />
        <Pill label="Kind" value={kind} onChange={setKind} options={kinds} />
        {data && (
          <span className="fbar-note">
            {polls.length === all.length ? `${all.length} polls` : `${polls.length} of ${all.length} polls`}
            {all[0] ? ` · latest ${fmtAgo(all[0].started_ms)}` : ""}
            {bad > 0 && <b className="st-crit"> · {bad} failed</b>}
          </span>
        )}
        <span className="grow" />
        <button className="btn btn-ghost btn-sm" onClick={() => reload()}>Refresh</button>
      </div>
      {loading && !data && <Loading />}
      {error && !data && <ErrorState error={error} retry={reload} />}
      {data && all.length === 0 && (
        <Empty title="No polls recorded yet">
          The log fills as soon as the collector runs: <code>python3 main.py watch --all</code>
        </Empty>
      )}
      {data && all.length > 0 && polls.length === 0 && (
        <Empty title="No poll matches the search and filters">
          They look through the latest {all.length} polls.
        </Empty>
      )}

      {polls.length > 0 && (
        <div className="panel log-panel">
          <table className="tbl log-tbl">
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
                  <tr key={p.poll_id} className={cls === "st-crit" ? "row-crit" : cls === "st-warn" ? "row-warn" : ""}>
                    <td className="nowrap" title={new Date(p.started_ms).toLocaleString()}>{fmtAgo(p.started_ms)}</td>
                    <td style={{ overflowWrap: "anywhere" }}>{p.label}</td>
                    <td>{p.kind}</td>
                    <td>{p.account ? `@${p.account}` : "—"}</td>
                    <td className="num">{p.pages}</td>
                    <td className="num">{p.new_tweets > 0 ? <b className="st-good">{p.new_tweets}</b> : p.new_tweets}</td>
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
  const [tab, setTab] = useState("timeline");
  const [q, setQ] = useState("");
  const [live, setLive] = useState(true);
  return (
    <>
      <PageHead title="Activity Log" onMenu={onMenu}
                sub={tab === "timeline" ? "Everything the tool did and everyone who touched it, to the second"
                                        : "The X poll history"}
                center={<HeadSearch value={q} onChange={setQ}
                          placeholder={tab === "timeline" ? "Search messages, IPs, accounts, keys…" : "Search streams, accounts…"} />}>
        {tab === "timeline" && (
          <button className="chip-live as-btn" onClick={() => setLive((v) => !v)}
                  title={live ? "Refreshing every 5 seconds — click to pause" : "Paused — click to resume"}>
            <span className={`dot ${live ? "pulse" : "off"}`} />{live ? "Live" : "Paused"}
          </button>
        )}
        <div className="seg">
          <button className={tab === "timeline" ? "on" : ""} onClick={() => setTab("timeline")}>Timeline</button>
          <button className={tab === "polls" ? "on" : ""} onClick={() => setTab("polls")}>X polls</button>
        </div>
      </PageHead>
      {tab === "timeline" ? <Timeline q={q} setQ={setQ} live={live} /> : <PollLog q={q} />}
    </>
  );
}
