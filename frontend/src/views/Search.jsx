// Search the archive — everything already collected, filtered locally.
// Nothing here spends X rate-limit budget.
import React, { useState } from "react";
import { api, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import CollectionPicker from "../components/CollectionPicker.jsx";
import PostCard from "../components/PostCard.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

export default function Search({ onMenu }) {
  const { project } = useProject();
  const pid = project?.project_id;
  const [form, setForm] = useState({ q: "", author: "", has_media: false, no_retweets: false, min_likes: "", scope: "project" });
  const [params, setParams] = useState(null);
  const [page, setPage] = useState(0);
  const [pinTarget, setPinTarget] = useState(null);
  const limit = 25;

  const results = useApi(
    () =>
      params
        ? api.tweets({ ...params, limit, offset: page * limit })
        : Promise.resolve(null),
    [params, page],
  );

  const run = () => {
    setPage(0);
    setParams({
      q: form.q || undefined,
      author: form.author || undefined,
      has_media: form.has_media ? "1" : undefined,
      no_retweets: form.no_retweets ? "1" : undefined,
      min_likes: form.min_likes || undefined,
      project: form.scope === "project" && pid ? pid : undefined,
    });
  };

  const f = (k) => (e) =>
    setForm((s) => ({ ...s, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  const total = results.data?.total ?? 0;
  const pages = Math.ceil(total / limit);

  return (
    <>
      <PageHead title="Search" onMenu={onMenu} sub="The archive — no rate-limit budget spent" />

      <div className="filters">
        <input placeholder='Text, e.g. "flood relief"' value={form.q} onChange={f("q")}
               style={{ flex: 2, minWidth: 180 }}
               onKeyDown={(e) => e.key === "Enter" && run()} />
        <input placeholder="@author" value={form.author} onChange={f("author")}
               style={{ width: 140 }} onKeyDown={(e) => e.key === "Enter" && run()} />
        <input placeholder="min likes" inputMode="numeric" value={form.min_likes}
               onChange={f("min_likes")} style={{ width: 100 }} />
        <label className="check">
          <input type="checkbox" checked={form.has_media} onChange={f("has_media")} /> has media
        </label>
        <label className="check">
          <input type="checkbox" checked={form.no_retweets} onChange={f("no_retweets")} /> no retweets
        </label>
        <select value={form.scope} onChange={f("scope")}>
          <option value="project">{project ? `Project: ${project.name}` : "This project"}</option>
          <option value="all">All projects</option>
        </select>
        <button className="btn btn-brand" onClick={run}>Search</button>
      </div>

      {!params && (
        <Empty title="Search everything you have collected">
          Filters combine — text, author, media, likes — and stay inside your own
          database.
        </Empty>
      )}
      {results.loading && params && !results.data && <Loading label="Searching…" />}
      {results.error && <ErrorState error={results.error} retry={results.reload} />}
      {results.data && (
        <>
          <div className="feed-head">
            <h2>{fmtN(total)} result{total === 1 ? "" : "s"}</h2>
            {pages > 1 && (
              <span className="right">
                <button className="btn btn-ghost btn-sm" disabled={page === 0}
                        onClick={() => setPage((p) => p - 1)}>← Prev</button>
                {" "}page {page + 1} / {pages}{" "}
                <button className="btn btn-ghost btn-sm" disabled={page + 1 >= pages}
                        onClick={() => setPage((p) => p + 1)}>Next →</button>
              </span>
            )}
          </div>
          {results.data.rows.length === 0 && (
            <Empty title="Nothing matches">Loosen a filter and try again.</Empty>
          )}
          {results.data.rows.map((t) => (
            <PostCard key={t.tweet_id} t={{ ...t, platform: "x" }} onPin={setPinTarget} />
          ))}
          {pinTarget && pid && (
            <CollectionPicker t={pinTarget} pid={pid} onClose={() => setPinTarget(null)} />
          )}
        </>
      )}
    </>
  );
}
