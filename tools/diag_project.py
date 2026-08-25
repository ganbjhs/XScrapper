#!/usr/bin/env python3
"""
Why did a project stop collecting? Read-only, safe on a live server.

    python3 diag_project.py "iSupportNamo" [/opt/xscraper/app/results.db]

Prints, per stream: the EXACT query string sent to X, the recent polls with
their stop reason and rate-limit state, and — the part that usually explains
it — each active collection filter checked against what this stream has
ACTUALLY been collecting. A filter that excludes every post the account
writes looks identical to "the account stopped posting" from the dashboard.
"""
import json
import sqlite3
import sys
import time

name = sys.argv[1] if len(sys.argv) > 1 else "iSupportNamo"
path = sys.argv[2] if len(sys.argv) > 2 else "/opt/xscraper/app/results.db"

db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
db.row_factory = sqlite3.Row

row = db.execute("SELECT project_id FROM projects WHERE name = ?", (name,)).fetchone()
if not row:
    have = [r["name"] for r in db.execute("SELECT name FROM projects")]
    sys.exit(f"no project {name!r}. projects on this server: {have}")
pid = row["project_id"]
print(f"PROJECT {name}  (id {pid})")
print("=" * 74)


def stream_ids_for(wl_id):
    pre = f"wl:{wl_id}:"
    return [r["stream_id"] for r in db.execute(
        "SELECT stream_id FROM streams WHERE label LIKE ?", (pre + "%",))]


def sample(sids):
    """What this watchlist has actually collected — the reality a filter is
    about to be judged against."""
    if not sids:
        return None
    q = ",".join("?" * len(sids))
    return db.execute(
        f"SELECT COUNT(*) n, "
        f"       SUM(CASE WHEN like_count >= 1 THEN 1 ELSE 0 END) liked, "
        f"       MAX(like_count) maxl, "
        f"       SUM(CASE WHEN is_retweet THEN 1 ELSE 0 END) rts, "
        f"       SUM(CASE WHEN is_reply THEN 1 ELSE 0 END) reps "
        f"FROM tweets t JOIN tweet_hits h USING(tweet_id) "
        f"WHERE h.stream_id IN ({q})", sids).fetchone()


for w in db.execute("SELECT * FROM watchlists WHERE project_id = ? ORDER BY watchlist_id", (pid,)):
    try:
        flt = json.loads(w["filters"] or "{}")
    except (TypeError, ValueError):
        flt = {}
    print(f"\nWATCHLIST {w['watchlist_id']}  {w['name']}   kind={w['kind']}")
    print(f"  filters active ({len(flt)}): {flt or 'none'}")

    sids = stream_ids_for(w["watchlist_id"])
    s = sample(sids)
    langs = []
    if sids:
        q = ",".join("?" * len(sids))
        langs = db.execute(
            f"SELECT lang, COUNT(*) n FROM tweets t JOIN tweet_hits h USING(tweet_id) "
            f"WHERE h.stream_id IN ({q}) GROUP BY lang ORDER BY n DESC LIMIT 5", sids).fetchall()

    if s and s["n"]:
        print(f"  reality check on {s['n']} posts already collected here:")
        shown = ", ".join("%s x%d" % (r["lang"], r["n"]) for r in langs)
        print(f"    languages : {shown}")
        print(f"    with >=1 like : {s['liked']}/{s['n']}   busiest post: {s['maxl']} likes")

        if flt.get("min_likes"):
            share = (s["liked"] or 0) / s["n"]
            print(f"  !! min_likes={flt['min_likes']} -> 'min_faves:{flt['min_likes']}'")
            print(f"     only {share:.0%} of this account's posts ever reach 1 like"
                  f" (best ever: {s['maxl']}). This filter alone can stop the stream dead.")
        if flt.get("min_retweets"):
            print(f"  !! min_retweets={flt['min_retweets']} -> same trap as min_likes.")
        if flt.get("lang"):
            seen = {r["lang"] for r in langs}
            bad = flt["lang"] not in seen
            mark = "!!" if bad else "  "
            print(f"  {mark} lang={flt['lang']} -> 'lang:{flt['lang']}'"
                  + (f"  BUT this stream has only ever collected {seen} — "
                     f"nothing will ever match." if bad else ""))
    else:
        print("  (no posts collected through this watchlist yet)")

    if flt.get("verified_only"):
        print("  !! verified_only -> 'filter:blue_verified'. If the handle is not")
        print("     blue-verified this returns ZERO forever, silently.")
    if flt.get("only_media") and flt.get("skip_links"):
        print("  !! only_media + skip_links together: a media post still carries a")
        print("     t.co link, so these two fight and can cancel out.")

print("\n" + "=" * 74)
for s in db.execute(
        "SELECT s.* FROM streams s JOIN project_streams ps USING(stream_id) "
        "WHERE ps.project_id = ? ORDER BY s.stream_id", (pid,)):
    q = s["query"] or f"(x-list {s['list_id']})"
    print(f"\nSTREAM {s['stream_id']}  {s['label']}    paused={s['paused']}  watched={s['watched']}")
    print(f"  QUERY SENT TO X:\n    {q}")
    polls = db.execute("SELECT * FROM polls WHERE stream_id = ? ORDER BY poll_id DESC LIMIT 8",
                       (s["stream_id"],)).fetchall()
    if not polls:
        print("  no polls recorded — this stream has never actually been checked")
        continue
    print(f"  {'when':<14}{'kind':<8}{'res':>4}{'new':>5}  {'stop_reason':<20}{'account':<16}rl_left")
    for p in polls:
        when = time.strftime("%m-%d %H:%M", time.localtime((p["started_ms"] or 0) / 1000))
        print(f"  {when:<14}{p['kind']:<8}{p['results']:>4}{p['new_tweets']:>5}  "
              f"{str(p['stop_reason']):<20}{str(p['account']):<16}{p['rl_remaining']}")
        if p["error"]:
            print(f"      ERROR: {p['error']}")

last = db.execute(
    "SELECT MAX(collected_ms) m FROM tweets t JOIN tweet_hits h USING(tweet_id) "
    "JOIN project_streams ps ON ps.stream_id = h.stream_id WHERE ps.project_id = ?",
    (pid,)).fetchone()["m"]
if last:
    print(f"\nNewest post stored for this project: "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last / 1000))}"
          f"   ({(time.time() * 1000 - last) / 3600000:.1f}h ago)")
