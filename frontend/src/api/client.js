// One thin fetch layer. Every endpoint is same-origin (the Python server or
// the Vite dev proxy), cookie-authed; a 401 means the session expired and the
// only useful response is the login page.

async function request(path, opts = {}) {
  const rep = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (rep.status === 401) {
    window.location.href = "/login";
    throw new Error("signed out");
  }
  let data;
  try {
    data = await rep.json();
  } catch {
    throw new Error(`HTTP ${rep.status}: not JSON`);
  }
  if (!rep.ok) throw new Error(data.error || `HTTP ${rep.status}`);
  // The API reports validation problems as {error} with HTTP 200; surface
  // them the same way as transport errors so callers handle one shape.
  if (data && typeof data === "object" && data.error) throw new Error(data.error);
  return data;
}

const qs = (p) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(p || {})) {
    if (v !== undefined && v !== null && v !== "" && v !== false) u.set(k, v);
  }
  const s = u.toString();
  return s ? `?${s}` : "";
};

export const api = {
  status: () => request("/api/status"),
  metrics: (project) => request(`/api/metrics${qs({ project })}`),
  guard: () => request("/api/guard"),
  activity: (p) => request(`/api/activity${qs(p)}`),
  activityLogs: (p) => request(`/api/activity/logs${qs(p)}`),
  delivery: (project) => request(`/api/delivery${qs({ project })}`),
  streamAssignments: () => request("/api/streams/assignments"),
  attachStream: (project, stream_id) =>
    request("/api/streams/attach", { method: "POST", body: { project, stream_id } }),
  detachStream: (project, stream_id) =>
    request("/api/streams/detach", { method: "POST", body: { project, stream_id } }),
  createDeliveryTarget: (body) =>
    request("/api/delivery/targets", { method: "POST", body }),
  updateDeliveryTarget: (body) =>
    request("/api/delivery/targets/update", { method: "POST", body }),
  removeDeliveryTarget: (target_id) =>
    request("/api/delivery/targets/remove", { method: "POST", body: { target_id } }),
  deliveryBackfill: (body) =>
    request("/api/delivery/backfill", { method: "POST", body }),
  testSheet: (body) =>
    request("/api/delivery/sheet/test", { method: "POST", body }),
  sheetScript: (body) =>
    request("/api/delivery/sheet/script", { method: "POST", body }),
  projectFetch: (project, ack) =>
    request("/api/project/fetch", { method: "POST", body: { project, ack } }),
  setCollection: (paused) =>
    request("/api/collection", { method: "POST", body: { paused } }),
  tweets: (p) => request(`/api/tweets${qs(p)}`),
  igPosts: (p) => request(`/api/ig/posts${qs(p)}`),
  igStatus: () => request("/api/ig/status"),
  igSource: (body) => request("/api/ig/source", { method: "POST", body }),
  igFetch: (project) => request("/api/ig/fetch", { method: "POST", body: { project } }),
  igControl: (action) => request("/api/ig/control", { method: "POST", body: { action } }),
  igSettings: (body) => request("/api/ig/settings", { method: "POST", body }),
  // Shared display-name identity: link handles across X/FB/IG so the picture is shared.
  identities: (platform) => request(`/api/identities${qs({ platform })}`),
  setIdentity: (platform, handle, display_name) =>
    request("/api/identity", { method: "POST", body: { platform, handle, display_name } }),
  // X List members — the individual accounts inside a list (cached; refresh spends a little budget).
  xlistMembers: (list_id) => request(`/api/watchlist/xmembers${qs({ list_id })}`),
  refreshXlistMembers: (list_id) =>
    request("/api/watchlist/xmembers/refresh", { method: "POST", body: { list_id, ack: true } }),
  // Stress test — find how many requests an account can pull before it's hot.
  // stressAccounts lists platforms+accounts; the UI works for any future
  // platform the server registers, no client change needed.
  stressAccounts: () => request("/api/stress/accounts"),
  stressRun: (body) => request("/api/stress/run", { method: "POST", body }),
  fbPosts: (p) => request(`/api/fb/posts${qs(p)}`),
  fbStatus: (project) => request(`/api/fb/status${qs({ project })}`),
  fbAddSource: (project, label) =>
    request("/api/fb/source", { method: "POST", body: { project, label, action: "add" } }),
  fbRemoveSource: (label) =>
    request("/api/fb/source", { method: "POST", body: { label, action: "remove" } }),
  fbSetInterval: (label, speed) =>
    request("/api/fb/source", { method: "POST", body: { label, action: "interval", speed } }),
  fbSetEnabled: (label, enabled) =>
    request("/api/fb/source", { method: "POST", body: { label, action: "enable", enabled } }),
  fbFetch: (project) =>
    request("/api/fb/fetch", { method: "POST", body: { project } }),
  fbFavorites: (project) =>
    request("/api/fb/favorites", { method: "POST", body: { project } }),
  fbControl: (action) =>
    request("/api/fb/control", { method: "POST", body: { action } }),
  fbHealthAction: (action) =>
    request("/api/fb/health", { method: "POST", body: { action } }),
  fbSettings: (body) =>
    request("/api/fb/settings", { method: "POST", body }),

  // --- Account Control Panel: the managed account pool (store_accounts) ---
  pool: () => request("/api/pool"),
  poolAdd: (body) => request("/api/pool/add", { method: "POST", body }),
  poolUpdate: (body) => request("/api/pool/update", { method: "POST", body }),
  poolRemove: (account_id) =>
    request("/api/pool/remove", { method: "POST", body: { account_id } }),
  poolStatus: (account_id, status, health) =>
    request("/api/pool/status", { method: "POST", body: { account_id, status, health } }),
  poolPromote: (account_id) =>
    request("/api/pool/promote", { method: "POST", body: { account_id } }),
  poolFailover: (platform, rotate_proxy) =>
    request("/api/pool/failover", { method: "POST", body: { platform, rotate_proxy } }),
  poolBackupCodes: (account_id, codes) =>
    request("/api/pool/backup_codes", { method: "POST", body: { account_id, codes } }),
  poolLogin: (account_id) =>
    request("/api/pool/login", { method: "POST", body: { account_id } }),
  poolTotp: (account_id) => request(`/api/pool/totp${qs({ account_id })}`),
  // Session import / background sign-in. `user_agent` is the operator's OWN
  // browser string, sent with a paste on purpose: the cookies were copied from
  // that browser, and a session that then collects under a different
  // user-agent is a fingerprint that has never been associated with it.
  poolSignin: (body) =>
    request("/api/pool/signin", { method: "POST",
                                  body: { ...body, user_agent: navigator.userAgent } }),
  poolSigninStatus: () => request("/api/pool/signin"),
  poolSigninHelp: () => request("/api/pool/signin/help"),

  projects: () => request("/api/projects"),
  createProject: (name) => request("/api/projects", { method: "POST", body: { name } }),
  archiveProject: (project_id, archived) =>
    request("/api/projects", { method: "POST", body: { project_id, archived } }),

  watchlists: (project) => request(`/api/watchlists${qs({ project })}`),
  createWatchlist: (body) => request("/api/watchlists", { method: "POST", body }),
  watchlistMembers: (watchlist_id, add, remove) =>
    request("/api/watchlists/members", { method: "POST", body: { watchlist_id, add, remove } }),
  removeWatchlist: (watchlist_id) =>
    request("/api/watchlists/remove", { method: "POST", body: { watchlist_id } }),
  watchlistFilters: (watchlist_id, filters) =>
    request("/api/watchlists/filters", { method: "POST", body: { watchlist_id, filters } }),
  watchlistInterval: (watchlist_id, seconds) =>
    request("/api/watchlists/interval", { method: "POST", body: { watchlist_id, seconds } }),

  streamSettings: (body) => request("/api/stream/settings", { method: "POST", body }),

  collections: (project) => request(`/api/collections${qs({ project })}`),
  createCollection: (project, name) =>
    request("/api/collections", { method: "POST", body: { project, name } }),
  removeCollection: (collection_id) =>
    request("/api/collections/remove", { method: "POST", body: { collection_id } }),
  collectionPin: (collection_id, add, remove) =>
    request("/api/collections/pin", { method: "POST", body: { collection_id, add, remove } }),
  collectionItems: (id) => request(`/api/collections/items${qs({ id })}`),

  alerts: (project) => request(`/api/alerts${qs({ project })}`),
  createAlert: (body) => request("/api/alerts", { method: "POST", body }),
  updateAlert: (body) => request("/api/alerts/update", { method: "POST", body }),
  removeAlert: (alert_id) =>
    request("/api/alerts/remove", { method: "POST", body: { alert_id } }),
};

// Tiny data hook: load-on-mount + optional polling + manual reload. Enough
// state for honest loading/empty/error UI without a cache library.
import { useCallback, useEffect, useRef, useState } from "react";

export function useApi(fn, deps = [], { every = 0 } = {}) {
  const [state, set] = useState({ data: null, error: null, loading: true });
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async (soft = false) => {
    if (!soft) set((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await fn();
      if (alive.current) set({ data, error: null, loading: false });
    } catch (e) {
      if (alive.current) set((s) => ({ data: soft ? s.data : null, error: String(e.message || e), loading: false }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    load();
    if (!every) return;
    const t = setInterval(() => load(true), every);
    return () => clearInterval(t);
  }, [load, every]);

  return { ...state, reload: load };
}

export const fmtN = (n) => (n == null ? "—" : Number(n).toLocaleString("en-IN"));

export const fmtLag = (ms) => {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 120_000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 7_200_000) return `${Math.round(ms / 60_000)}m`;
  return `${Math.round(ms / 3_600_000)}h`;
};

export const fmtAgo = (iso) => {
  const t = typeof iso === "number" ? iso : Date.parse(iso);
  if (!t) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

// Beyond this, "how long ago" stops being the useful answer.
const AGO_LIMIT_DAYS = 30;

/**
 * When a post was PUBLISHED, for a human reading a feed.
 *
 * Relative up to a month ("3h ago", "12d ago"), then the actual date. The
 * cutover exists because the two questions are different: for something from
 * this week "how fresh is it" is what you want, but "97d ago" answers nothing
 * — nobody counts backwards from today to find July, and a feed showing a
 * month of history turns into a column of three-digit day counts you have to
 * do arithmetic on.
 *
 * Deliberately NOT folded into fmtAgo. That one measures OUR freshness —
 * when we last collected, last delivered, last polled — where a big number is
 * itself the alarm and a date would hide it. This measures someone else's
 * timeline.
 */
export const fmtPosted = (iso) => {
  const t = typeof iso === "number" ? iso : Date.parse(iso);
  if (!t) return "—";
  const days = (Date.now() - t) / 86_400_000;
  if (days < AGO_LIMIT_DAYS) return fmtAgo(t);
  return new Date(t).toLocaleDateString("en-IN", {
    day: "numeric", month: "short", year: "numeric",
  });
};
