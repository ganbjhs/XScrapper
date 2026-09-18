# Prompt for the Watch-Tower agent — an Instagram panel in Collector

> **Superseded 2026-09-18 by `WATCH_TOWER_INSTAGRAM_STREAM_PROMPT.md`** — Instagram
> is now served as a stream on `/api/tweets`, so the separate-panel ask below
> is no longer what to send. Kept for the record.

*Paste the fenced block below to the Watch-Tower side verbatim. It is written
for their coding agent.*

## Context for us, not for them (2026-09-18)

Watch-Tower judged the all-platform redesign (`WATCH_TOWER_COLLECTOR_PANEL_PROMPT.md`)
too deep a change to their core. What they asked for instead: **Instagram
only, shown separately from X**, i.e. its own tab/section in Connections →
Collector rather than a row under the existing X project cards. Their X
cards, their mirror and their binding for X stay exactly as they are. This
note is that smaller ask.

They already have the Instagram *pull* (`WATCH_TOWER_INSTAGRAM_PROMPT.md`,
2026-09-02: `/api/ig/status`, `/api/ig/posts`, 72h-window loop). What is
missing is the panel and the bind, so a client project can receive Instagram
from us at all.

Not in this note: the X label bug (`wl:N:0` read as a project id — see the
all-platform file §0), Facebook, per-list selection. Those wait until they are
ready for the bigger change.

---

```
Add an INSTAGRAM section to Connections → Collector, separate from your X
cards. Your X cards, X mirror and X bindings do not change. Same host
(https://scraper.vedictech.in), same Bearer key, same GET paths you already
hold. Everything below is readable today; nothing new was opened.

You were sent the Instagram pull on 2 Sept (/api/ig/status, /api/ig/posts,
the 72h-window loop). This note adds the panel and the binding so that a
Watch-Tower project can actually receive Instagram from us.

## 1. What an Instagram watchlist is, on our side

We have no "Instagram list" object the way X has a List. An Instagram
watchlist is simply the set of Instagram SOURCES (handles) that live in one
upstream PROJECT. So the natural unit for your panel is:

    one Instagram watchlist  =  one upstream project_id P that has ≥1 source

Discover them:

    GET /api/projects
      -> { projects:[{project_id,name,archived,watchlists,streams}] }
    for each non-archived P:
    GET /api/ig/status?project=P
      -> { sources:[{label,type,value,platform_id,project_id,
                     account,assigned_account,enabled,collector}],
           totals:{posts,sources_enabled,newest_taken_at,sources_unresolved},
           paused, config:{interval_s}, accounts:[…], unassigned }

    A project whose sources[] is empty has no Instagram watchlist — skip
    it in the Instagram panel (it may still have X lists; that is your X
    panel's business). Today three projects qualify:
      P=13 "Varanasi"                        10 sources
      P=14 "Rajasthan WatchList"            118 sources
      P=17 "BhajanLal Ji Watchlist Media 2"  25 sources
    Re-discover on every Refresh; we add projects and sources without
    telling you.

    `project` is REQUIRED on /api/ig/status and /api/ig/posts. Without it
    you get {"error":"no project selected"} with HTTP 200 — always check
    for an `error` key.

## 2. The panel

Collector page → a second tab or section titled "Instagram", beside the
existing X list cards. One card per Instagram watchlist (= per P):

    header   upstream project name · "upstream #P" · rename (your alias,
             like the X cards) · your bound Watch-Tower project or "NOT USED"
    numbers  {sources_enabled} sources · {totals.posts} posts upstream ·
             newest post {totals.newest_taken_at} · {sources_unresolved}
             pending · paused yes/no · "checked every {config.interval_s}s"
             · your mirrored count · your last run
    sources  the full list, expandable, one row per source where enabled:
               @{value} · label (our cross-platform person name; on these
               projects it is usually the handle itself) ·
               type (user | hashtag | following) ·
               numeric id {platform_id} or the words "id pending" when it is
               "" — that means we have not resolved the handle yet and it
               has produced 0 posts so far; it is our problem, not yours;
               show it, do not alarm on it ·
               "via @{collector}" (which of our accounts reads it)
             `account` and `assigned_account` are internals — show only
             `collector`. Ignore accounts[] and unassigned entirely.
    actions  Bind (choose a Watch-Tower project) · Unbind · Backfill (from
             YOUR mirror) · Mirror now (run the pull for this P)
             NO "Fetch now": /api/ig/fetch is dashboard-only (403 by design)
             and our Instagram collector paces itself like a human. Do not
             probe /api/ig/source, /api/ig/control, /api/ig/settings either.

Handles are the members. There is no separate members call for Instagram
— sources[].value IS the account list, and it is complete.

## 3. Binding

Bind = (your Watch-Tower project) ↔ (our project_id P, Instagram). It is
independent of any X binding you hold on the same P: a client may take X
from P=14 and not Instagram, or Instagram only, or both. Two separate
relations, two separate Unbind buttons, no coupling. If the same
Watch-Tower project is bound to P for X (on your X card) and to P for
Instagram (on this card), it simply receives both.

Bound means your mirror runs the Instagram pull for P — the loop you were
already given, unchanged:

    cold load / backfill:
      cursor=None; loop GET /api/ig/posts?project=P&limit=200[&cursor=<c>]
        upsert by `id` (string); stop when count<200 or next_cursor null
    incremental, every 5 minutes:
      cursor=None; loop GET /api/ig/posts?project=P&limit=200&since=72h[&cursor]
        upsert by `id`; stop when count<200 or next_cursor null
      store your own last-run time as the "cursor" — there is no upstream
      collection cursor on Instagram yet; the 72h overlap is what makes it
      gapless (we can pick a post up hours or days after it was posted)

Post shape and the mapping to your X-shaped row are in the 2 Sept note
and have not changed (id is a STRING; media URLs are Instagram CDN links
that expire — render promptly or mirror the thumbnail bytes at ingest;
metrics are frozen at first collection).

Membership is ours to track: when we add or remove a source in P, the
bound watchlist keeps flowing with its current members and you change
nothing. Never turn sources[] into a per-handle poll (`username=` exists on
/api/ig/posts for debugging, not for ingestion) — that is the same mistake
as the 45-handle list on X in August.

Unbind stops the pull for that (project, P). Keep the mirrored rows or
drop them — your call, as with X.

## 4. Verify, then report back

  1. GET /api/status with the Bearer header -> 200.
  2. The Instagram tab shows exactly the projects whose /api/ig/status
     sources[] is non-empty — today 13, 14 and 17 — and NO card for a
     project with X lists but no Instagram sources.
  3. Card 17 shows 25 sources, all "id pending", 0 posts upstream. Card 14
     shows 118 sources, most with a numeric id, posts > 0, and a
     newest_taken_at within the last day (Indian daytime).
  4. Bind a test Watch-Tower project to P=14 (Instagram). After Mirror now
     your row count for it equals `total` on the first page of
     GET /api/ig/posts?project=14&limit=200. Open your Live Feed with the
     Instagram filter on that project: thumbnails and captions render.
  5. Leave the incremental loop running 30 minutes during Indian daytime:
     new rows appear within ~5 minutes of newest_taken_at moving.
  6. Unbind: the pull stops (no further /api/ig/posts?project=14 calls from
     your side in our access log); your X binding on P=14, if any, is
     untouched.
  7. Report per bound P: your Instagram row count vs our totals.posts, and
     the distinct `source` values you hold.

  If you want a `since_collected_ms` cursor on /api/ig/posts so the loop is
  identical to X, say so — it is a small change on our side.
```

---

## Open items on our side

- Their sidebar already has a "Instagram" entry under Connections (their own
  connected-accounts feature, not us). Suggest they name the new section
  "Instagram (Collector)" or keep it as a tab inside Collector so the two are
  not confused. Said in §2 as "tab or section"; leave the naming to them.
- `since_collected_ms` on `/api/ig/posts` is offered for the second time.
  If they ask, do it together with the Facebook filter/cursor already listed
  in the all-platform file.
- Project 17's 25 sources are all `id pending` (see
  `claude/ig-project17-id-pending-2026-09-17.md`). Their card will show 0
  posts for it until the ids resolve or are pasted; the prompt tells them
  that is expected, but it will be the first question they ask.
