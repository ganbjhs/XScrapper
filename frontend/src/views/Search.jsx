// Search the archive — everything already collected, platform-wise.
// X-Advanced-Search-style boxes (comma-separated); the backend (search.py)
// builds the query, so there is no syntax to learn. Nothing here touches a
// platform or spends rate-limit budget.
import React, { useEffect, useState } from "react";
import { api, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import CollectionPicker from "../components/CollectionPicker.jsx";
import PostCard from "../components/PostCard.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

const PLATS = [["all", "All"], ["x", "X"], ["instagram", "Instagram"], ["facebook", "Facebook"]];
const NAME = { x: "X", instagram: "Instagram", facebook: "Facebook" };

const BOXES = [
  ["all", "All of these words", "flood relief patna"],
  ["phrase", "This exact phrase", "flood relief camp"],
  ["any", "Any of these", "modi, rahul gandhi"],
  ["none", "None of these", "cricket, ipl"],
  ["tags", "Hashtags (any of)", "#bihar, #patna"],
  ["from", "From these accounts", "@narendramodi, @_samvad_news_"],
  ["mention", "Mentioning", "@PMOIndia"],
];

const EMPTY = {
  all: "", phrase: "", any: "", none: "", tags: "", from: "", mention: "",
  min_likes: "", min_comments: "", min_views: "", has_media: false,
  no_reposts: false, no_replies: false, from_date: "", to_date: "", sort: "latest",
  // one-platform filters
  x_types: [], lang: "", verified: false, min_followers: "", min_reposts: "", has_link: false,
  ig_types: [], min_shares: "",
};

function countText(c) {
  if (!c) return "0";
  return `${fmtN(c.n)}${c.plus ? "+" : ""}`;
}

export default function Search({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const [platform, setPlatform] = useState("all");
  const [form, setForm] = useState(EMPTY);
  const [scope, setScope] = useState("project");
  const [params, setParams] = useState(null);
  const [stack, setStack] = useState([]);       // cursors of the pages before this one
  const [cursor, setCursor] = useState(null);
  const [counts, setCounts] = useState(null);
  const [pinTarget, setPinTarget] = useState(null);

  const results = useApi(
    () => (params ? api.search({ ...params, cursor: cursor || undefined }) : Promise.resolve(null)),
    [params, cursor],
  );
  // Counts come with the first page only; keep them while paging.
  useEffect(() => { if (results.data?.counts) setCounts(results.data.counts); }, [results.data]);

  const set = (k) => (e) =>
    setForm((s) => ({ ...s, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const toggle = (k, v) => () =>
    setForm((s) => ({ ...s, [k]: s[k].includes(v) ? s[k].filter((x) => x !== v) : [...s[k], v] }));

  const build = () => {
    const p = { platform };
    for (const k of ["all", "phrase", "any", "none", "tags", "from", "mention",
                     "min_likes", "min_comments", "min_views", "from_date", "to_date"]) {
      if (String(form[k]).trim()) p[k] = String(form[k]).trim();
    }
    for (const k of ["has_media", "no_reposts", "no_replies"]) if (form[k]) p[k] = "1";
    if (form.sort !== "latest") p.sort = form.sort;
    // A platform's own filters travel only when that platform is chosen.
    if (platform === "x") {
      if (form.x_types.length) p.x_types = form.x_types.join(",");
      if (form.lang.trim()) p.lang = form.lang.trim();
      if (form.verified) p.verified = "1";
      if (form.has_link) p.has_link = "1";
      if (form.min_followers.trim()) p.min_followers = form.min_followers.trim();
      if (form.min_reposts.trim()) p.min_reposts = form.min_reposts.trim();
    }
    if (platform === "instagram" && form.ig_types.length) p.ig_types = form.ig_types.join(",");
    if (platform === "facebook" && form.min_shares.trim()) p.min_shares = form.min_shares.trim();
    if (scope === "project" && pid) p.project = pid;
    else p.scope = "all";
    return p;
  };

  const run = () => {
    setStack([]); setCursor(null); setCounts(null);
    setParams(build());
  };
  // Switching platform re-runs a search that is already showing.
  useEffect(() => { if (params) run(); }, [platform]); // eslint-disable-line react-hooks/exhaustive-deps
  const clear = () => { setForm(EMPTY); setParams(null); setCounts(null); setStack([]); setCursor(null); };
  const next = () => { setStack((s) => [...s, cursor]); setCursor(results.data.cursor); };
  const prev = () => { setStack((s) => s.slice(0, -1)); setCursor(stack[stack.length - 1] || null); };
  const onKey = (e) => e.key === "Enter" && run();

  const d = results.data;
  const total = counts ? Object.values(counts).reduce((a, c) => a + c.n, 0) : 0;
  const plus = counts ? Object.values(counts).some((c) => c.plus) : false;
  const csvHref = params ? api.searchCsvUrl(params) : null;
  const num = (k, ph, w = 104) => (
    <input placeholder={ph} inputMode="numeric" value={form[k]} onChange={set(k)}
           onKeyDown={onKey} style={{ width: w }} />
  );
  const check = (k, label) => (
    <label className="check"><input type="checkbox" checked={form[k]} onChange={set(k)} /> {label}</label>
  );
  const multi = (k, opts) => opts.map((v) => (
    <label key={v} className="check">
      <input type="checkbox" checked={form[k].includes(v)} onChange={toggle(k, v)} /> {v}
    </label>
  ));

  return (
    <>
      <PageHead title="Search" onMenu={onMenu} sub="Everything already collected · no rate-limit budget spent" />

      <div className="tabs">
        {PLATS.map(([k, n]) => (
          <button key={k} className={`tab ${platform === k ? "sel" : ""}`}
                  onClick={() => setPlatform(k)}>{n}</button>
        ))}
      </div>

      <div className="panel">
        <div className="field"
             style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", columnGap: 14 }}>
          {BOXES.map(([k, label, ph]) => (
            <div key={k}>
              <label>{label}</label>
              <input value={form[k]} placeholder={ph} onChange={set(k)} onKeyDown={onKey} />
            </div>
          ))}
        </div>

        <div className="filters" style={{ marginTop: 14 }}>
          {num("min_likes", "min likes")}
          {num("min_comments", "min comments", 120)}
          {num("min_views", "min views")}
          {check("has_media", "has media")}
          {check("no_reposts", "hide reposts")}
          {check("no_replies", "hide replies")}
          <input type="date" value={form.from_date} onChange={set("from_date")} title="From (IST day)" />
          <input type="date" value={form.to_date} onChange={set("to_date")} title="To (IST day, inclusive)" />
        </div>

        {platform === "x" && (
          <div className="filters">
            {multi("x_types", ["original", "reply", "quote", "repost"])}
            <input placeholder="language (en, hi)" value={form.lang} onChange={set("lang")}
                   onKeyDown={onKey} style={{ width: 130 }} />
            {check("verified", "verified")}
            {check("has_link", "has link")}
            {num("min_followers", "min followers", 120)}
            {num("min_reposts", "min reposts")}
          </div>
        )}
        {platform === "instagram" && (
          <div className="filters">{multi("ig_types", ["photo", "video", "reel", "album"])}</div>
        )}
        {platform === "facebook" && (
          <div className="filters">{num("min_shares", "min shares")}</div>
        )}

        <div className="filters" style={{ marginBottom: 0 }}>
          <select value={form.sort} onChange={set("sort")}>
            <option value="latest">Latest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="project">{project ? `Project: ${project.name}` : "This project"}</option>
            <option value="all">All projects</option>
          </select>
          <button className="btn btn-brand" onClick={run}>Search</button>
          <button className="btn btn-ghost" onClick={clear}>Clear</button>
          {csvHref && <a className="btn btn-ghost" href={csvHref}>Export CSV</a>}
        </div>
      </div>

      {!params && (
        <Empty title="Search everything you have collected">
          Fill any box. Commas separate items. Dates are whole IST days.
        </Empty>
      )}
      {results.loading && params && !d && <Loading label="Searching…" />}
      {results.error && <ErrorState error={results.error} retry={results.reload} />}
      {d && (
        <>
          <div className="feed-head" style={{ flexWrap: "wrap" }}>
            <h2>{fmtN(total)}{plus ? "+" : ""} result{total === 1 && !plus ? "" : "s"}</h2>
            {counts && (
              <span className="muted">
                {Object.entries(counts).map(([k, c]) => `${NAME[k]} ${countText(c)}`).join(" · ")}
              </span>
            )}
            {d.partial && <span className="chip warn" title="Stopped at the time limit — showing what was found">partial</span>}
            <span className="right">
              <button className="btn btn-ghost btn-sm" disabled={!stack.length}
                      onClick={prev}>← Prev</button>
              {" "}page {stack.length + 1}{" "}
              <button className="btn btn-ghost btn-sm" disabled={!d.cursor}
                      onClick={next}>Next →</button>
            </span>
          </div>
          {(d.notes?.length > 0 || d.errors) && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
              {(d.notes || []).map((n) => <div key={n}>{n}</div>)}
              {Object.entries(d.errors || {}).map(([k, e]) => (
                <div key={k} className="st-crit">{NAME[k] || k}: {e}</div>
              ))}
            </div>
          )}
          {d.rows.length === 0 && <Empty title="Nothing matches">Loosen a box or a filter.</Empty>}
          {d.rows.map((t) => (
            <PostCard key={`${t.platform}:${t.tweet_id}`} t={t} onPin={setPinTarget} />
          ))}
          {pinTarget && pid && (
            <CollectionPicker t={pinTarget} pid={pid} onClose={() => setPinTarget(null)} />
          )}
        </>
      )}
    </>
  );
}
