// The main screen: incoming posts for this project, stat strip, and the
// pipe-health column (delivery, chart, watchlists). Polls every 10s and
// batches new arrivals behind a "N new posts" pill so the list never jumps.
import React, { useEffect, useMemo, useState } from "react";
import { api, fmtAgo, fmtLag, fmtN, useApi } from "../api/client.js";
import { PageHead, useProject } from "../App.jsx";
import CollectedChart from "../components/CollectedChart.jsx";
import CollectionPicker from "../components/CollectionPicker.jsx";
import PostCard from "../components/PostCard.jsx";
import { Empty, ErrorState, Loading } from "../components/ui.jsx";

const normIg = (p) => ({
  platform: "instagram",
  tweet_id: p.id,
  url: p.url,
  text: p.text,
  created_at: p.created_at,
  collected_at: p.collected_at || p.created_at,
  author_username: p.author?.username,
  author_display_name: p.author?.username,
  // Server-side backfill: the X avatar for the same handle (one photo
  // everywhere — X is the canonical avatar source).
  author_avatar: p.author_avatar || p.author?.avatar,
  like_count: p.metrics?.likes,
  reply_count: p.metrics?.comments,
  view_count: p.metrics?.views,
  media: p.media?.type
    ? [{ type: p.media.type, url: p.media.video || p.media.thumbnail, thumb: p.media.thumbnail }]
    : [],
});

const DUR_MS = { "1h": 36e5, "6h": 216e5, "12h": 432e5, "24h": 864e5,
                 "48h": 1728e5, "7d": 6048e5, "30d": 2592e6 };
const DUR_LABEL = { "1h": "Last 1 hour", "6h": "Last 6 hours", "12h": "Last 12 hours",
                    "24h": "Last 24 hours", "48h": "Last 48 hours",
                    "7d": "Last 7 days", "30d": "Last 30 days", all: "All time" };

function Pill({ label, value, onChange, options }) {
  return (
    <label className="fpill">
      <span>{label}:</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, text, disabled]) => (
          <option key={v} value={v} disabled={disabled}>{text}</option>
        ))}
      </select>
    </label>
  );
}

export default function LiveFeed({ onMenu }) {
  const { project, projectsError } = useProject();
  const pid = project?.project_id;
  const [flt, setFlt] = useState({ source: "all", sort: "latest", dur: "24h" });

  const metrics = useApi(() => api.metrics(pid), [pid], { every: 30_000 });
  const status = useApi(() => api.status(), [], { every: 30_000 });
  const delivery = useApi(() => api.delivery(pid), [pid], { every: 15_000 });
  const wls = useApi(() => (pid ? api.watchlists(pid) : Promise.resolve({ watchlists: [] })), [pid]);

  // Load-more: every platform is fetched at pageN × PAGE and the duration
  // filter now applies to ALL three (Instagram/Facebook used to ignore it).
  const PAGE = 30;
  const [pageN, setPageN] = useState(1);
  useEffect(() => { setPageN(1); }, [pid, flt.dur, flt.sort, flt.source]);

  const feed = useApi(
    async () => {
      const lim = pageN * PAGE;
      const since = flt.dur !== "all" ? flt.dur : undefined;
      const [x, ig, fb] = await Promise.all([
        pid
          ? api.tweets({
              project: pid, limit: lim, since,
              sort: flt.sort === "likes" || flt.sort === "views" ? flt.sort : undefined,
              order: flt.sort === "oldest" ? "asc" : undefined,
            })
          : Promise.resolve({ rows: [] }),
        api.igPosts({ limit: lim, since }).catch(() => ({ posts: [] })),
        pid ? api.fbPosts({ project: pid, limit: lim, since }).catch(() => ({ posts: [] }))
            : Promise.resolve({ posts: [] }),
      ]);
      const rows = [
        ...(x.rows || []).map((r) => ({ ...r, platform: "x" })),
        ...(ig.posts || []).map(normIg),
        ...(fb.posts || []),   // already in feed shape (store_fb.to_feed)
      ];
      rows.sort((a, b) => Date.parse(b.collected_at || 0) - Date.parse(a.collected_at || 0));
      // *Total is the SERVER's count for the window — the TRUE number, not the
      // page size, so the "N posts" figure is real for every platform.
      return {
        rows,
        xTotal: x.total ?? (x.rows || []).length,
        igTotal: ig.total ?? (ig.posts || []).length,
        fbTotal: fb.total ?? (fb.posts || []).length,
      };
    },
    [pid, flt.dur, flt.sort, pageN],
    // The stream below is the real-time path; this refetch is the safety net
    // that also picks up Instagram (which the stream does not carry yet).
    { every: 60_000 },
  );

  // Real-time: an event stream from the server pushes each post the moment
  // it is stored. Posts arriving here merge with the fetched backlog.
  const [pushed, setPushed] = useState([]);
  const [liveOk, setLiveOk] = useState(false);
  const [pinTarget, setPinTarget] = useState(null);
  const [fetching, setFetching] = useState(false);
  const [fetchMsg, setFetchMsg] = useState("");

  // Refresh = ask X for the newest posts RIGHT NOW (one page per stream),
  // then the collector's normal cadence carries on. Not just a re-read.
  const refreshNow = async (ack = false) => {
    if (!pid || fetching) return;
    setFetching(true);
    setFetchMsg("");
    // Facebook runs a headless browser and takes ~a minute, so kick it off in
    // the BACKGROUND — don't make the fast X refresh wait on it. When it lands,
    // reload the feed and note how many Facebook posts arrived.
    api.fbFetch(pid)
      .then((fr) => {
        if (fr && fr.new > 0) {
          setFetchMsg(`✓ ${fr.new} new from Facebook`);
          feed.reload(true);
        }
      })
      .catch(() => { /* FB may be mid-run or unconfigured — never block X */ });
    // Instagram, same idea: fire in the background so it never blocks X.
    api.igFetch(pid)
      .then((ir) => {
        if (ir && ir.new > 0) {
          setFetchMsg(`✓ ${ir.new} new from Instagram`);
          feed.reload(true);
        }
      })
      .catch(() => { /* IG may be paused / not signed in — never block X */ });
    try {
      const r = await api.projectFetch(pid, ack);
      setFetchMsg(`✓ ${r.new} new from X · checking Facebook…`);
      feed.reload(true);
      metrics.reload(true);
    } catch (e) {
      const msg = String(e.message || e);
      if (!ack && msg.startsWith("Warnings not acknowledged") &&
          window.confirm(`${msg.replace("Warnings not acknowledged: ", "Guard warning:\n")}\n\nFetch anyway?`)) {
        setFetching(false);
        return refreshNow(true);
      }
      setFetchMsg(`✗ ${msg}`);
    } finally {
      setFetching(false);
      setTimeout(() => setFetchMsg(""), 6000);
    }
  };
  useEffect(() => {
    if (!pid) return;
    setPushed([]);
    const es = new EventSource(`/api/live?project=${pid}`);
    es.onopen = () => setLiveOk(true);
    es.onerror = () => setLiveOk(false);   // EventSource retries by itself
    es.addEventListener("post", (e) => {
      try {
        const t = { ...JSON.parse(e.data), platform: "x" };
        setPushed((p) =>
          p.some((x) => String(x.tweet_id) === String(t.tweet_id)) ? p : [t, ...p]);
      } catch { /* one bad frame must not kill the stream */ }
    });
    return () => { es.close(); setLiveOk(false); };
  }, [pid]);

  // Batch new arrivals: the visible list only advances when the pill is
  // clicked, so reading is never interrupted by a reflow. When nothing is on
  // screen yet (first load, project switch, empty feed) there is nothing to
  // interrupt — reveal immediately.
  const [shownIds, setShownIds] = useState(null);
  const feedRows = feed.data?.rows;
  const latest = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const t of [...pushed, ...(feedRows || [])]) {
      const k = `${t.platform}:${t.tweet_id}`;
      if (!seen.has(k)) { seen.add(k); out.push(t); }
    }
    out.sort((a, b) => Date.parse(b.collected_at || 0) - Date.parse(a.collected_at || 0));
    return out;
  }, [pushed, feedRows]);
  const keyOf = (t) => `${t.platform}:${t.tweet_id}`;
  // The filter bar applies to everything on screen — fetched backlog and
  // stream-pushed posts alike (the server pre-filters the backlog; this
  // repeats the rule locally so live arrivals obey it too).
  const filtered = useMemo(() => {
    const cutoff = flt.dur !== "all" ? Date.now() - DUR_MS[flt.dur] : 0;
    // Some sources (Facebook) don't expose an exact post time, so created_at is
    // null. Fall back to collected time for the window test, and if even that
    // won't parse, keep the post rather than silently hiding it — a just-
    // collected post must never vanish from a recent-window view.
    const inWindow = (t) => {
      if (!cutoff) return true;
      // Show if the post OR its collection falls in the window. Facebook posts
      // can carry an older original time but were just collected — those must
      // not vanish from a recent view. If neither timestamp parses, keep it.
      const c = Date.parse(t.created_at || "");
      const g = Date.parse(t.collected_at || "");
      if (!Number.isNaN(c) && c >= cutoff) return true;
      if (!Number.isNaN(g) && g >= cutoff) return true;
      return Number.isNaN(c) && Number.isNaN(g);
    };
    const out = latest.filter((t) =>
      (flt.source === "all" || t.platform === flt.source) && inWindow(t));
    const by = {
      latest: (a, b) => Date.parse(b.collected_at || 0) - Date.parse(a.collected_at || 0),
      oldest: (a, b) => Date.parse(a.collected_at || 0) - Date.parse(b.collected_at || 0),
      likes: (a, b) => (b.like_count || 0) - (a.like_count || 0),
      views: (a, b) => (b.view_count || 0) - (a.view_count || 0),
    }[flt.sort];
    return by ? [...out].sort(by) : out;
  }, [latest, flt]);
  const visible = useMemo(
    () => (shownIds ? filtered.filter((t) => shownIds.has(keyOf(t))) : filtered),
    [filtered, shownIds],
  );
  useEffect(() => {
    setShownIds((prev) => {
      if (!latest.length) return prev;
      // First paint shows everything; afterwards the refetched backlog is
      // never "new" — only stream-pushed posts wait behind the pill.
      if (!prev) return new Set(latest.map(keyOf));
      const next = new Set(prev);
      for (const t of feedRows || []) next.add(keyOf(t));
      return next.size === prev.size ? prev : next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latest, feedRows]);
  const fresh = shownIds ? filtered.length - visible.length : 0;

  const m = metrics.data;
  // The pid of a live collector process — the one honest signal that posts
  // can actually arrive. Accounts being signed in is NOT that signal.
  const watcherUp = Boolean(status.data?.watcher_pid);
  const paused = Boolean(status.data?.collection_paused);
  const toggleCollection = async () => {
    await api.setCollection(!paused);
    status.reload(true);
  };
  // Every term from this project's KEYWORD watchlists, so the feed can
  // underline where each keyword-search hit actually matched. "a AND b" is two
  // terms; surrounding quotes are stripped so a phrase highlights as the phrase.
  const keywordTerms = useMemo(() => {
    const set = new Set();
    for (const w of wls.data?.watchlists || []) {
      if (w.kind !== "keywords") continue;
      for (const m of w.members || []) {
        for (const piece of String(m.handle || "").split(/\s+AND\s+/i)) {
          const term = piece.trim().replace(/^["']+|["']+$/g, "").trim();
          if (term.length >= 2) set.add(term);
        }
      }
    }
    return [...set];
  }, [wls.data]);
  const wlCount = wls.data?.watchlists?.length ?? 0;
  const handleCount = (wls.data?.watchlists || []).reduce((a, w) => a + w.members.length, 0);
  const wtTargets = (delivery.data?.targets || []).filter((t) => t.kind === "webhook");
  const behind = wtTargets.reduce((a, t) => a + (t.behind || 0), 0);

  return (
    <>
      <PageHead title="Live Feed" onMenu={onMenu}
                sub={project ? `${project.name} — ${handleCount} handles · ${wlCount} watchlists` : "No project yet"}>
        <span className="chip-live">
          <span className={`dot${!status.data ? " off" : !watcherUp ? " bad" : paused ? " warn" : liveOk ? " pulse" : ""}`} />
          {!status.data ? "…" : !watcherUp ? "Collection off"
            : paused ? "Paused" : liveOk ? "Live" : "Collecting"}
        </span>
        {fetchMsg && (
          <span style={{ fontSize: 12.5, fontWeight: 600 }}
                className={fetchMsg.startsWith("✓") ? "st-good" : "st-crit"}>
            {fetchMsg}
          </span>
        )}
        {watcherUp && (
          <button className="btn btn-ghost" onClick={toggleCollection}>
            {paused ? "▶ Start collection" : "⏸ Pause collection"}
          </button>
        )}
        <button className="btn btn-brand" disabled={fetching} onClick={() => refreshNow()}>
          {fetching ? "Fetching from X…" : "Refresh"}
        </button>
      </PageHead>

      {status.data && !watcherUp && (
        <div className="banner-crit" role="alert">
          <b>Collection is OFF.</b> This page is showing what was collected
          earlier — nothing new will arrive and nothing is being sent to
          Watch-Tower. Start the collector service on the server once:
          <code style={{ marginLeft: 6 }}>systemctl start xscraper-watch</code>
        </div>
      )}
      {status.data && watcherUp && paused && (
        <div className="banner-crit" role="alert"
             style={{ borderLeftColor: "var(--warning)", background: "color-mix(in srgb, var(--warning) 8%, var(--surface))" }}>
          <b style={{ color: "var(--warn-text)" }}>Collection is paused.</b> Nothing
          new is being collected. Click <b>▶ Start collection</b> above to resume.
        </div>
      )}

      <section className="stats">
        <div className="stat">
          <div className="k">Collected today</div>
          <div className="v">{fmtN(m?.today?.collected)}</div>
          <div className="d">{fmtN(m?.today?.photos)} photos · {fmtN(m?.today?.videos)} videos</div>
        </div>
        <div className="stat">
          <div className="k">Median lag</div>
          <div className="v">{m?.today?.median_lag_ms != null ? fmtLag(m.today.median_lag_ms) : "—"}</div>
          <div className="d">post → collected · p95 {m?.today?.p95_lag_ms != null ? fmtLag(m.today.p95_lag_ms) : "—"}</div>
        </div>
        <div className="stat">
          <div className="k">Watching</div>
          <div className="v">{fmtN(handleCount)}</div>
          <div className="d">handles across {wlCount} watchlist{wlCount === 1 ? "" : "s"}</div>
        </div>
        <div className="stat">
          <div className="k">Sent to Watch-Tower</div>
          {wtTargets.length === 0 ? (
            <>
              <div className="v" style={{ fontSize: 18 }}>Not set up</div>
              <div className="d">declare a [[webhooks]] target</div>
            </>
          ) : behind === 0 ? (
            <>
              <div className="v st-good" style={{ fontSize: 20 }}>✓ In sync</div>
              <div className="d">cursor 0 behind</div>
            </>
          ) : (
            <>
              <div className="v st-warn" style={{ fontSize: 20 }}>⚠ {fmtN(behind)} behind</div>
              <div className="d">delivery is catching up</div>
            </>
          )}
        </div>
      </section>

      <div className="fbar">
        <Pill label="Source" value={flt.source}
              onChange={(v) => setFlt((s) => ({ ...s, source: v }))}
              options={[["all", "All"], ["x", "X / Twitter"],
                        ["instagram", "Instagram"], ["facebook", "Facebook"]]} />
        <Pill label="Sort" value={flt.sort}
              onChange={(v) => setFlt((s) => ({ ...s, sort: v }))}
              options={[["latest", "Latest first"], ["oldest", "Oldest first"],
                        ["likes", "Most liked"], ["views", "Most viewed"]]} />
        <Pill label="Duration" value={flt.dur}
              onChange={(v) => setFlt((s) => ({ ...s, dur: v }))}
              options={Object.entries(DUR_LABEL)} />
      </div>

      <div className="cols">
        <section>
          <div className="feed-head">
            <h2>Incoming</h2>
            <span className="newpill" style={{ cursor: "default" }} title="posts matching the filters above">
              {fmtN(
                flt.source === "x"
                  ? feed.data?.xTotal ?? 0
                  : flt.source === "instagram"
                    ? feed.data?.igTotal ?? 0
                    : flt.source === "facebook"
                      ? feed.data?.fbTotal ?? 0
                      : (feed.data?.xTotal ?? 0) + (feed.data?.igTotal ?? 0) +
                        (feed.data?.fbTotal ?? 0),
              )}{" "}posts
            </span>
            {fresh > 0 && (
              <button className="newpill"
                      onClick={() => setShownIds(new Set(latest.map(keyOf)))}>
                ▲ {fresh} new post{fresh === 1 ? "" : "s"}
              </button>
            )}
            <span className="right">
              {{ latest: "Newest first · by collected time",
                 oldest: "Oldest first · by collected time",
                 likes: "Most liked first",
                 views: "Most viewed first" }[flt.sort]}
              {" · "}{DUR_LABEL[flt.dur].toLowerCase()}
            </span>
          </div>

          {projectsError && <ErrorState error={projectsError} />}
          {feed.loading && !feed.data && <Loading label="Loading the feed…" />}
          {feed.error && !feed.data && <ErrorState error={feed.error} retry={feed.reload} />}
          {feed.data && visible.length === 0 && (
            wlCount === 0 ? (
              <Empty title="This project isn't watching anything yet">
                Create a watchlist under <b>Watchlists</b> — collection starts
                automatically{watcherUp ? " within a minute" : " once the collector is on"},
                no commands needed.
              </Empty>
            ) : (
              <Empty title="Nothing in this view yet">
                {watcherUp
                  ? "Collection is running. Try Refresh for an immediate fetch, or widen Duration to “All time” to see older posts."
                  : "The collector is off (see the banner above) — nothing new can arrive until it starts."}
              </Empty>
            )
          )}
          {visible.map((t) => (
            <PostCard key={`${t.platform}:${t.tweet_id}`} t={t}
                      onPin={setPinTarget} terms={keywordTerms} />
          ))}
          {feed.data && visible.length > 0 && (() => {
            const total = flt.source === "x" ? (feed.data.xTotal ?? 0)
              : flt.source === "instagram" ? (feed.data.igTotal ?? 0)
              : flt.source === "facebook" ? (feed.data.fbTotal ?? 0)
              : (feed.data.xTotal ?? 0) + (feed.data.igTotal ?? 0) + (feed.data.fbTotal ?? 0);
            return visible.length < total ? (
              <div style={{ textAlign: "center", margin: "16px 0 4px" }}>
                <button className="btn btn-ghost" disabled={feed.loading}
                        onClick={() => setPageN((n) => n + 1)}>
                  {feed.loading ? "Loading…"
                    : `Load more — showing ${fmtN(visible.length)} of ${fmtN(total)}`}
                </button>
              </div>
            ) : null;
          })()}
          {pinTarget && pid && (
            <CollectionPicker t={pinTarget} pid={pid} onClose={() => setPinTarget(null)} />
          )}
        </section>

        <aside>
          <div className="panel">
            <div className="phead"><h3>Delivery to Watch-Tower</h3><span className="right">webhook</span></div>
            {delivery.loading && !delivery.data && <div className="sub" style={{ color: "var(--ink-3)" }}>Loading…</div>}
            {delivery.data && wtTargets.length === 0 && (
              <div className="kv"><span>No webhook targets</span><b>see config.toml.example</b></div>
            )}
            {wtTargets.map((t) => (
              <div key={t.label}>
                <div className="kv"><span>{t.label} → {t.url}</span><b /></div>
                <div className="kv"><span>Cursor behind</span>
                  <b className={t.behind ? "st-warn" : "st-good"}>{fmtN(t.behind)} posts</b></div>
                <div className="kv"><span>Delivered</span><b>{fmtN(t.sent)}</b></div>
                <div className="kv"><span>Last success</span>
                  <b>{t.last_ok_ms ? fmtAgo(t.last_ok_ms) : "never"}</b></div>
                {t.failures > 0 && (
                  <div className="kv"><span>Failing</span>
                    <b className="st-crit">{t.failures}× — {t.last_error}</b></div>
                )}
              </div>
            ))}
            {(delivery.data?.targets || []).filter((t) => t.kind === "telegram").map((t) => (
              <div className="kv" key={t.label}>
                <span>Telegram · {t.streams[0]}</span>
                <b className={t.behind ? "st-warn" : "st-good"}>
                  {t.behind ? `${fmtN(t.behind)} queued` : "✓ in sync"}
                </b>
              </div>
            ))}
          </div>

          <div className="panel">
            <div className="phead"><h3>Collected per day</h3><span className="right">Last 7 days</span></div>
            <CollectedChart perDay={m?.per_day} />
          </div>

          <div className="panel">
            <div className="phead"><h3>Watchlists</h3><span className="right">this project</span></div>
            {(wls.data?.watchlists || []).length === 0 && (
              <div className="kv"><span>None yet</span><b>create one under Watchlists</b></div>
            )}
            {(wls.data?.watchlists || []).map((w) => (
              <div className="wl-row" key={w.watchlist_id}>
                <div className="who">
                  <b>{w.name}</b>
                  <small>
                    {w.kind === "xlist"
                      ? `X List ${w.list_id}`
                      : `${w.members.length} handles · ${w.streams.filter((s) => !s.paused).length} stream${w.streams.filter((s) => !s.paused).length === 1 ? "" : "s"}`}
                  </small>
                </div>
                <div className="right">
                  {fmtN(w.streams.reduce((a, s) => a + (s.tweets || 0), 0))} collected
                </div>
              </div>
            ))}
          </div>
        </aside>
      </div>
    </>
  );
}
