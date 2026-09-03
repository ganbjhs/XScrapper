# Watch-Tower — the consumer this tool exists for

*Read this BEFORE touching delivery, the API surface, the post shape, project
or watchlist scoping, media handling, labels, retention, or anything else a
downstream consumer can feel. `RULEBOOK.md` says what we may not do to
ourselves; this file says what we may not do to Watch-Tower, and why.*

*Written 2 Sep 2026 from the live app (`https://app.watch-tower.in/`, admin
view, Collector / Projects / Live Feed pages and its `/api/collector`
response) and from our own source. Watch-Tower is a separate product with its
own team and its own coding agent; we do not have their code. Where a
statement below is inferred from the UI rather than read from source, it says
so. Keep this file current the same way `RULEBOOK.md` §0 demands: a change
that alters what Watch-Tower receives updates this file in the same commit.*

---

## 0. The one-line relationship

**Watch-Tower is the analysis product. We are its collector.** Everything we
store is raw material for their sentiment, stance, topics, reach, alerts and
reports. They pull it from us with an API key, project by project, into a
mirror of their own, and bind each of our projects to one of theirs. Our job
ends at "correct, complete, fast, stable-shaped, project-scoped posts with
working media URLs". Theirs starts there. (`RULEBOOK.md` §1 directive 2.)

---

## 1. What Watch-Tower is

A hosted "Social Media Intelligence" dashboard for political and corporate
clients in India (`app.watch-tower.in`). Observed in the admin UI:

**Structure.** Around fifty **projects** (ABVP, HDFC, Bihar_Watchlist, BJP
Rajasthan Watchlist, SIDBI, Devendra Fadnavis Ji, …). A project is one of two
types: a **keyword project** (chips like `HDFC, एचडीएफसी, Housing Development
Finance`, fetched by Watch-Tower itself on a 15m–24h interval with a "max
tweets / fetch" cap) or a **watchlist project** (no keywords; fed entirely by
a bound upstream Collector project — ours). Each project has status
Active/Inactive, a "client context & narrative brief" that guides their AI,
per-source on/off toggles, an optional **watchlist-keywords filter** ("all
tracked handles are still fetched — only the posts containing one of these
keywords are kept"), **exclude replies**, **exclude handles / keywords**, an
**AI relevance filter** (off-topic mentions auto-hidden "from feed & KPIs, no
data lost"), and a **Filter 1 engagement gate** — low-engagement posts are
dropped unless the author is on the project's own *watchlist* of handles
("these accounts always bypass Filter 1").

**Sources.** X/Twitter, Instagram, Facebook, YouTube, Reddit, LinkedIn,
Google News, Newspaper, TV, and "Media Monitoring". Their **own** fetches for
X, Facebook, Instagram, Reddit and LinkedIn are **paid per result** (the
Backfill dialog prices an X backfill at "about ₹630 at the cap"; Google News
and YouTube are free). **That is why we exist: the Collector replaces the
paid X feed for watchlist projects, and is meant to do the same for
Instagram and Facebook.**

**Features that consume our posts** (sidebar): Live Feed (results / positive /
negative / neutral counts, reach, sentiment and stance tags such as
`POSITIVE` / `SUPPORT`, an AI one-liner per post, Top Topics with re-grouping,
a "Noise" toggle, 7-day results-and-reach chart), Watchlist, Influencers
(Top by Reach, Verified / Negative / Support Voices, Engagement Score, AI
classification), Saved searches, Reports (weekly SM report, MMR report,
Newspaper report, combined MMR, Share Live Link), Activity Log,
Notifications and Alerts, Radar (Crisis Radar), Knowledge ("Brain": paste
text, links, PDFs), Explorer, Spikes, Share of Voice, Themes, Negative Surge,
Campaign Tracker, Report Archive, Compare, Error Logs, Health Monitor, War
Room, Political Intelligence, TV / Newspaper / News(X) monitoring, Team.

Every one of those is analysis. None of it is ours to replicate.

---

## 2. How Watch-Tower ingests from us (the "Collector" page)

Connections → **Collector** (`GET /api/collector` on their side). One card per
**upstream project** — our `project_id`, which they call
`scraper_project_id`. Per card:

| Their field | Meaning | Our source |
|---|---|---|
| `scraper_project_id`, `scraper_name` | our project id and name (they may `rename` a display name locally) | `GET /api/projects` |
| `lists[]` (`label`, `source`, `paused`, `tweets`, `list_id`) | our stream labels e.g. `wl:15:0` and X List ids | `GET /api/watchlists?project=P` |
| `handles`, `posts_24h`, `top_handles[]`, `sample_capped` | 24h sample of authors and volume ("a `+` means the sample hit its page cap") | `GET /api/tweets?project=P&since_collected_ms=…` |
| `mirror_enabled`, `mirror_cursor`, `mirror_last_run`, `mirror_last_error`, `mirrored`, `mirrored_24h`, `mirror_oldest`, `mirror_newest`, `last_post_at` | their mirror of our project; **`mirror_cursor` is our `collected_ms`** (e.g. `1788363997000`) | the cursor contract in §4 R3 |
| `role` | `bound` (feeds a Watch-Tower project), `news` (project 8 "News" → feeds *every* project through Media Monitoring → News(X/Twitter)), `unused` (mirrored, bound to nothing) | — |
| `binding` (`project_id`, `project_name`, `project_type` = `watchlist` \| `keyword`, `cursor`, `pending`, `last_run_at`, `last_count`, `last_error`) | the Watch-Tower project this upstream project fills; `cursor` is a row id **inside their mirror**, `pending` is rows mirrored but not yet pushed into the project | — |

Buttons: **Mirror now** (`POST /api/collector/mirror`: pull from us into the
mirror), **Refresh stats**, per card **Fetch now**, **Backfill…**, **Bind /
Unbind** to a Watch-Tower project. "Every list is pulled once into our own
mirror; projects are then filled from that copy, so a backfill never touches
the collector again."

Snapshot 2 Sep 2026: ten upstream projects mirrored (2, 3, 7, 8, 9, 11, 12,
13, 14, 15); four bound (15 → Bihar_Watchlist, 13 → Varanasi_Watchlist,
14 → BJP Rajasthan Watchlist, 12 → Devendra Fadnavis Ji /keyword); 8 is
`news`; the rest `unused`. The News mirror held 1,09,300 rows.

**The ingest loop they run per project (as we prescribed in
`WATCH_TOWER_PROMPT.md` and as `mirror_cursor` confirms):**

```
GET /api/tweets?project=P&since_collected_ms=<cursor>&limit=500
Authorization: Bearer <key>
→ upsert by tweet_id, cursor = max(collected_ms) committed, page until < limit
```

Instagram is the same key and project ids on `/api/ig/posts` with a different
shape and paging model — `WATCH_TOWER_INSTAGRAM_HANDOVER.md`. The signed
webhook push (`webhook.py`) is an alternative delivery path that exists and
is documented (`WATCH_TOWER_HANDOVER.md`) but is **not** what the Collector
page is built on; the pull is. Do not assume a webhook target is what keeps
them fed.

`api.py` (`/v1/instagram/posts`, port 8790) is a standalone key service that
is **not deployed** as a systemd unit; the live Instagram surface is
`/api/ig/posts` in `web.py`. Do not document `/v1/…` to them.

---

## 3. What they do with a post, and therefore which of our fields matter

Inferred from the UI, not from their code:

| Their feature | Our fields it depends on |
|---|---|
| Sentiment / stance / AI insight / topics / themes | `text`, `lang` |
| Reach, Top by Reach, Influencers | `author_followers` (X), `author_username`, `author_display_name`, `author_avatar` |
| Filter 1 (engagement gate), Engagement Score, Most/Least engaged sort, Spikes, Negative Surge | `like_count`, `retweet_count`, `reply_count`, `quote_count`, `view_count` (IG: `metrics.likes/comments/views`) |
| Exclude replies | `is_reply` (our parsed field, not X's `-filter:replies` hint) |
| Exclude handles | `author_username` exact |
| Latest/Oldest sort, Duration window, 7-day chart | `created_at` (post time) — **not** `collected_at` |
| Gapless mirror | `collected_ms` + `tweet_id` composite cursor |
| Post card media, reports, PDF export | `media[]` (`type`, `url`, `thumb`); Facebook `thumb`/`url` are served **by us** (`/media/fb/…`) and must stay resolvable |
| Open-on-X / permalink | `url` |
| Dedupe | `tweet_id` / `id` as **string** |
| Per-project cards, binding, watchlist labels | `project_id` (stable), `streams[]` labels `wl:P:n`, `/api/watchlists` `kind` / `owner_handle` |
| Content category chips (if they show ours) | `label`, `label_source`, `label_ms` |

Watch-Tower's own **watchlist** (handles that bypass Filter 1) is *their*
per-project handle list and has nothing to do with our watchlists/streams.
Do not confuse the two words when talking to them: ours is a *collection
source*, theirs is an *analysis exemption*.

---

## 4. The compatibility rules (what we owe them)

**R1 — `project_id` is the join key. Never renumber, reuse or merge one.**
Their cards, mirrors and bindings are keyed on our integer id. Archiving must
keep the id; deleting a project orphans a binding on their side, so it is a
coordinated action, never a cleanup. Names are display only — they rename
locally ("upstream calls it 'Varanasi'").

**R2 — ids are strings and mean one thing forever.** `tweet_id` / `id` is the
platform's id as a string. Never synthesise, prefix or re-key it; they
de-duplicate on it and at-least-once delivery depends on it.

**R3 — the cursor contract is sacred.** `since_collected_ms` +
`limit ≤ 500`, **oldest-first while cursoring**, every row that exists with
`collected_ms > cursor` is returned, nothing is filtered inside the cursored
query, engagement sorts are ignored while a cursor is present, and
`collected_ms` of a stored row is **never rewritten** (a "fix" that touches
it makes a row re-deliver or vanish). `since_id` stays available but is
documented as lossy; do not make it the default. Instagram has no such cursor
yet — see §7.

**R4 — shape changes are additive only.** Add fields; never rename, remove,
retype (`null` vs `0` vs `""` included) or reorder semantics of an existing
one. A post they parsed yesterday must parse tomorrow. If a breaking change is
unavoidable: bump `PAYLOAD_VERSION` (webhook) / add a versioned path (API),
keep the old one serving, tell them, then retire on a date. `RULEBOOK.md` §2
"one post shape" is the base; this rule extends it to the wire.

**R5 — engagement and follower numbers ship as numbers.** A `null`
`like_count` is not "zero", it is "unknown", and their Filter 1 will treat it
as an engagement failure; `author_followers` `null` zeroes reach. Where the
platform gave the number, deliver it; where the platform has no such concept
(IG retweets), deliver `null` and document it. `is_reply` / `is_retweet` /
`is_quote` are booleans, never missing.

**R6 — media URLs must work when they open them, not just when we stored
them.** X and Instagram CDN URLs are theirs to fetch (RULEBOOK §1 directive
3); Facebook URLs expire in ~5 days so we re-host (`fb_media.py`) and must
deliver **absolute** `https://scraper.vedictech.in/media/fb/…` URLs, keep them
serving without cookie or key, and never rename the path scheme. Instagram
CDN URLs also carry an expiry (`oe=`) — until we re-host them, say so in
every Instagram note (done in the Instagram handover).

**R7 — a key sees one project at a time and only the allowed paths.**
`API_KEY_READ_PATHS` / `API_KEY_WRITE_PATHS` in `web.py` are the whole
surface. Instagram/Facebook without `?project=` return nothing (RULEBOOK §2).
Do not add a cross-project read for keys, do not widen `POST` beyond
`/api/fetch`, and when you add a path they should use, add it to the allowlist
**and** to `WATCH_TOWER_PROMPT.md` in the same change — a 403 body already
lists `allowed_get` / `allowed_post`, so the prompt must match it.

**R8 — reads for them are local and cheap; they never spend our platform
budget by accident.** `/api/tweets`, `/api/ig/posts`, `/api/fb/posts` read
SQLite. `/api/fetch` spends real X rate limit and is the one write they may
call; steady-state ingest should call it zero times (they were told). Never
add an endpoint that fetches live from a platform as a side effect of a read.

**R9 — no analysis leaves here except the one label.** No sentiment, no
score, no summary, no "relevance" flag in any payload. The single exception
is `label` / `label_source` / `label_ms` (RULEBOOK §1 directive 2) and it
travels as a fact with provenance, not as an opinion. If they ask for more,
the answer is "that is yours".

**R10 — errors are explicit in the body, and the wart is not to be
extended.** Every error carries an `error` key and a `detail`. Known wart:
`/api/ig/posts` and `/api/fb/posts` answer the "no project selected" error
with HTTP **200**. Do not add more 200-with-error paths; when fixing it,
warn them first (their client may key on the body, or on the status).

**R11 — time is UTC on the wire, IST only on screens.** `created_at` /
`collected_at` are ISO-8601 with offset (`+00:00` or `Z`); cursors are epoch
**milliseconds**; Instagram `taken_at` is epoch **seconds** internally and
must be converted before it leaves. Never emit a naive local timestamp.

**R12 — retention never outruns the slowest consumer.** `retention_days`
deletes rows; a row deleted before their `mirror_cursor` reached it is gone
from them forever, and pull consumers do not tell us their cursor. Keep
`retention_days = 0` or ≥ 7; use `raw_retention_days` (drops only the raw
JSON, keeps the row) as the size knob. Before enabling row retention, read
their `mirror_cursor`/`mirror_last_run` on the Collector page.

**R13 — availability is part of the contract.** Dozens of their projects
poll every few minutes; `/api/tweets?project=…` must stay indexed and fast,
and a deploy must not leave `web.py` down for longer than their retry
tolerance. The `xscraper-watch` outage story is "posts arrive late"; a
`web.py` outage story is "every Watch-Tower project stalls". Treat them
differently.

**R14 — every change they can feel is announced through the two files.**
`WATCH_TOWER_HANDOVER.md` (webhook) and `WATCH_TOWER_PROMPT.md` (API surface,
allowed paths, cursor rules) plus `WATCH_TOWER_INSTAGRAM_*.md` are the
documents their agent reads. A change to a path, a field, a status code, a
limit, or an allowlist updates the relevant file in the same commit and is
sent to them before it is deployed. If it is not written there, from their
point of view it did not happen.

---

## 5. Pre-change checklist (run it before touching anything in §4's scope)

1. Which of their features (§3 table) read the thing I am changing?
2. Is the change additive? If not, what is the versioning plan and the date?
3. Does any existing row's `collected_ms`, `tweet_id`, `project_id` or
   stream label change? If yes, stop — that is a re-delivery or a gap.
4. Does a key-callable path gain or lose a field, a parameter, a status
   code or a limit? Update `WATCH_TOWER_PROMPT.md` / the Instagram files.
5. Does the allowlist change? Same commit updates the prompt file.
6. Does any media URL scheme change? Old URLs must keep serving.
7. Can this fetch from a platform as a side effect of a GET? Remove that.
8. Does it add analysis to a payload? Remove that (R9).
9. Does it delete rows or shorten retention? Check their cursor first (R12).
10. Tests: `tests/test_all.py` holds the contract tests —
    `test_api_key_allowlist` (which paths a key may read/write),
    `test_projects_watchlists` and `test_keywords_and_project_delivery`
    (project scoping, `tweets_after` ordering, the delivered tweet shape),
    `test_backfilled_posts_reach_delivery` (late-collected rows still
    deliver — the reason R3 exists). A contract change grows a test.
11. Tell them: send the updated file, ask for the reconciliation numbers
    (row counts per project, distinct authors) after they deploy.

---

## 6. Vocabulary map (say the right word to the right side)

| Ours | Theirs | Notes |
|---|---|---|
| project (`project_id`) | upstream project / "source" card, `scraper_project_id` | join key, R1 |
| watchlist (`kind` = `xlist` \| `query` \| `keywords`), stream label `wl:P:n` | "list" on the card (`lists[]`) | ours collects, theirs displays |
| — | Watch-Tower project (`keyword` \| `watchlist` type) | what a card is **bound** to |
| — | their **watchlist** of handles | Filter 1 bypass list; unrelated to ours |
| `collected_ms` | `mirror_cursor` | ms epoch |
| `/api/tweets?project=P&since_collected_ms=…` | "Mirror now" / mirror loop | |
| — | "Fetch now" / "Backfill" on a card | push mirror rows into the bound project; never touches us |
| project 8 "News" | "News Outlets", role `news`, `news_scraper_project` | feeds all projects via Media Monitoring |
| content `label` | (their sentiment/stance are separate and theirs) | R9 |
| `media[]` `thumb` | post card image | R6 |

---

## 7. Known gaps and the wishlist (ours to fix, in priority order)

1. **Instagram has no collection-time cursor.** `/api/ig/posts` pages
   newest-first on `id`; `since` is post time. Their loop must use an overlap
   window (documented). Fix: `collected_at` in `store_ig.to_api` +
   `since_collected_ms` with oldest-first ordering in `store_ig.query` /
   `_ig_posts`. ~20 lines + a test; makes the IG loop identical to X.
2. **Instagram metrics are frozen** (`INSERT OR IGNORE`). Fine for
   monitoring, wrong for engagement growth. Fix only if they ask; it changes
   the "never update" invariant their upsert relies on (R4 applies).
3. **Instagram CDN media expires.** Extend `fb_media.py`'s re-hosting to
   Instagram thumbnails if reports/PDF exports start showing broken images.
   Note RULEBOOK §1 directive 3 ("never bytes") already has the Facebook
   exception and needs the same wording for Instagram if we do this.
4. **200-with-error** on `/api/ig/posts` and `/api/fb/posts` (R10).
5. **`author=` on `/api/tweets` is a prefix `LIKE`**, not equality
   (`author=ani` matches `ani_digital`). They were moved off per-handle
   polling, so it no longer bites them, but it is a correctness bug for any
   consumer; changing it is R4-breaking, so announce it.
6. **`/api/watchlist/xmembers` is empty** until
   `POST /api/watchlist/xmembers/refresh` is run; they were told not to use
   it. Either populate it or remove it from the allowlist and the prompt.
7. **Two Instagram read surfaces exist** (`/api/ig/posts` in `web.py`,
   `/v1/instagram/posts` in `api.py`). One is deployed. Retire or clearly
   mark the other so nobody documents it to them.
8. `/api/delivery` exposes raw webhook URLs (Apps Script `/exec` URLs are
   capability URLs) to a key — mask to host if that is not wanted.

---

## 8. Where to look

- `web.py` — `API_KEY_READ_PATHS` / `API_KEY_WRITE_PATHS`, `_query_tweets`
  (the `/api/tweets` cursor and sort rules), `_ig_posts`, `_fb_posts`,
  `_delivery_json`, `_stamp_labels`.
- `store.py` — `tweets_after` (the cursor), `parse_window`, project /
  watchlist / stream tables.
- `store_ig.py` — `to_api` (their Instagram shape), `to_feed` (our shape),
  `query` (`before_pk` cursor).
- `webhook.py` — `_tweet_json`, `sign` / `verify`, `pump` (push path).
- `fb_media.py` — the re-hosted media rule.
- `WATCH_TOWER_HANDOVER.md`, `WATCH_TOWER_PROMPT.md`,
  `WATCH_TOWER_INSTAGRAM_HANDOVER.md`, `WATCH_TOWER_INSTAGRAM_PROMPT.md` —
  what they have been told. Keep them true.
