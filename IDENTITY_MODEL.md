# The identity model: one person, three handles, three ids

**Status:** implemented for Instagram. Designed, not yet implemented, for Facebook and X.

## The rule

Every source row holds three separate facts, and they are never allowed to share a column:

| column | what it is | example | lifetime | who writes it |
|---|---|---|---|---|
| `label` | the **person** | `Narendra Modi` | forever | **a human, only** |
| `value` | the **handle** on this platform | `narendramodi` | changes on rename | a human |
| `platform_id` | the platform's **numeric id** | `1550693326` | forever | the collector |

`label` is the join key. It is what makes "the same person on Instagram, Facebook and X" a single
row in the operator's head and a single `GROUP BY` in the database. It is also the only one of the
three with no backup and no way to re-derive it — so nothing automated is permitted to write it.

`platform_id` is a **cache derived from `value`**. If the handle changes, the cached id is stale and
must be dropped, because an id that points at a name this row no longer claims will happily collect
the wrong person's posts under the right person's identity — the worst failure available here,
because it is silent.

## Why this exists

Instagram grants **name lookup** and **media reads** as separate permissions. On a session with an
open checkpoint, `user_medias_paginated_v1('1550693326')` returns posts while
`user_id_from_username('narendramodi')` returns `login_required`. Measured, not theorised — it is
what took the whole "Binay Kumar Singh" project offline on Aug 20, one `RetryError` per source per
pass, all night.

With one column doing both jobs, that left an impossible choice: store the handle and the fetch
breaks, or store the id and lose the cross-platform mapping. The split retires the choice. It is not
an Instagram quirk — every platform here has a readable name and an opaque internal id, and every
one of them will eventually gate them differently.

## How Instagram implements it

- `store_ig.SCHEMA` — `sources.platform_id`, with the invariant written above the table.
- `store_ig.Store._migrate` — adds the column to an existing file on open, so an old database
  upgrades itself rather than raising on the first row read.
- `store_ig.Source.user_id` — returns `platform_id or value`. **A row with no id still fetches by
  name.** This is what makes the change safe to deploy: nothing breaks on the first pass, rows
  simply stop failing as they fill in.
- `store_ig.Store.add_source` — the `ON CONFLICT` clause keeps a cached id when the handle is
  unchanged and drops it when the handle moves. A numeric `--value` is routed into `platform_id`,
  so the old calling convention still does the right thing.
- `store_ig.Store.cache_platform_id(handle, pk)` — keyed on the **handle**, not the label, because
  the engine only ever learns the name it was asked to resolve. Two projects watching the same
  account both benefit from one lookup.
- `engine_ig.resolve_user` — three independent ways in (private API → web `web_profile_info` →
  profile HTML), then `on_resolved` writes the answer to the store. The write-back is the part that
  matters: without it the cache dies with the process and a restarting service re-resolves, and
  re-fails, every name on every pass.
- `collect_ig.py set-id` / `resolve-ids` — the human escape hatch and the paced bulk pass.
- `migrate_ig_sources.py` — the one-time data move, deliberately manual.

## Applying it to Facebook

`store_fb.sources.label` currently holds **the page handle** (`narendramodi`), which means Facebook
has no identity column at all — the person and the handle are the same string, and the same person
is a different string on each platform. That is the thing to fix, and it is a bigger change than
Instagram's because `label` is a foreign key: `posts.source_label`, `removed_pages.handle` and
`page_profiles.handle` all key off it.

Proposed shape:

```sql
ALTER TABLE sources ADD COLUMN value       TEXT NOT NULL DEFAULT '';  -- page handle
ALTER TABLE sources ADD COLUMN platform_id TEXT NOT NULL DEFAULT '';  -- numeric page id
-- backfill: value = label, for every existing row
```

Then `label` is free to be promoted to the person, one row at a time, by a human. Until a row is
promoted, `label == value` and everything behaves exactly as it does today.

Sequencing matters here — do **not** promote labels before the backfill, or `posts.source_label`
stops joining:

1. add both columns, backfill `value = label`, ship. No behaviour change.
2. switch every read that means *the handle* from `label` to `value`
   (`collect_fb`, `engine_fb`, the favourites auto-register at `collect_fb.py:251`).
3. only then let the dashboard rename a `label` to a person, and add a
   `posts.source_label → sources.label` rename cascade, or key posts on `value` instead.

`page_profiles` is worth a second look while you are in there: it is keyed on `handle`, but
`display_name` and `avatar_url` are **identity** facts, not platform facts. Once `label` is the
person, that table wants to be keyed on the person too — which is exactly the "fetch the avatar
once from X, use it on all three" saving described below.

## Applying it to X

X is already most of the way there. `store.watchlist_members` has:

```
handle        TEXT NOT NULL,   -- lowercase, no '@'   <- value
display_name  TEXT,            -- the person          <- label
user_id       TEXT,            -- filled when resolved <- platform_id
```

Three columns, three meanings, already separated. Two gaps:

- `display_name` is whatever X reports, not a name an operator chose, so it does not reliably match
  the Instagram/Facebook `label`. It needs to become an editable identity field, or gain an
  `identity` column beside it.
- the primary key is `(watchlist_id, handle)`, so a rename orphans the row. Keying on the identity
  and treating `handle` as mutable would match the model.

## What the model buys, beyond fixing the bug

One `label` across three platforms means one profile, not three:

- **one avatar fetch.** Pull the picture once (X exposes it most cheaply) and serve it for the
  Instagram and Facebook rows too. Three profile requests per person become one — and profile
  requests are precisely the ones Instagram gates hardest.
- **one display name**, chosen by an operator, instead of three platform-supplied strings that
  disagree about spelling and capitalisation.
- **cross-platform queries become a join, not a heuristic.** "Everything Narendra Modi posted
  today, all platforms" is `WHERE label = ?` rather than three handle lists maintained by hand.
- **renames stop being outages.** A handle change updates one column; the identity, the history and
  every mapping survive.

## The one rule to keep

> The collector may write `platform_id`. A human writes `label` and `value`. Nothing automated ever
> writes `label`.

Every failure mode this document exists to prevent is a violation of that line.
