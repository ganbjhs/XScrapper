#!/usr/bin/env python3
"""
migrate_ig_sources.py — bring an existing ig_results.db onto the three-column
source model (label / value / platform_id), in place and without touching a
single identity.

RUN THIS ON THE MACHINE THAT HOLDS THE DATABASE (the server), once:

    python3 migrate_ig_sources.py --db ig_results.db          # report only
    python3 migrate_ig_sources.py --db ig_results.db --apply  # do it

WHAT CHANGED AND WHY THERE IS A MIGRATION AT ALL
------------------------------------------------
`sources.value` used to hold whatever the fetcher needed: sometimes a handle
('narendramodi'), sometimes a numeric id ('1550693326'). One column, two
meanings, and no way to have both — which is why a source keyed by the handle
died on any restricted session (Instagram gates username lookup separately from
media reads; see engine_ig.resolve_user) while the identical source keyed by id
collected fine.

The fix splits the meanings apart:

    label        the PERSON      "Narendra Modi"    <- the cross-platform key
    value        the HANDLE      narendramodi       <- readable, stable, yours
    platform_id  the NUMERIC ID  1550693326         <- a cache, machine-only

store_ig.Store adds the column automatically when it opens an old file, so the
SCHEMA half of this needs no help. What needs a decision — and therefore a
human running a script — is the DATA half: rows whose `value` is a number are
carrying an id in the handle column, and moving it is a judgement about what
that row meant. This script makes that move explicit, reversible (it backs the
file up first) and safe to re-run.

WHAT IT DOES
  1. adds the platform_id column if it is missing
  2. for every `user` row whose value is all digits and whose platform_id is
     empty: moves the number into platform_id and clears value, because that
     row never had a handle to begin with — the number was standing in for one
  3. leaves every other row completely alone
  4. prints the rows that still need a numeric id, which is the worklist for
     `collect_ig.py resolve-ids`

WHAT IT NEVER DOES
  * write to `label`. Not once, under any flag. Your identity mapping between
    Instagram, Facebook and X is the one thing here that has no backup.
  * overwrite a platform_id that is already set.
  * delete a row.

--seed lets you type ids you already know, for accounts a restricted session
cannot look up:

    python3 migrate_ig_sources.py --db ig_results.db --apply \\
        --seed "Narendra Modi=1550693326" --seed "Yogi Adityanath=..."
"""

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def _cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="ig_results.db", help="path to ig_results.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--seed", action="append", default=[], metavar="LABEL=ID",
                    help="cache a numeric id you already know; repeatable")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}")
        return 1

    seeds = {}
    for pair in args.seed:
        if "=" not in pair:
            print(f"--seed wants LABEL=ID, got {pair!r}")
            return 1
        lab, _, pk = pair.partition("=")
        lab, pk = lab.strip(), pk.strip()
        if not pk.isdigit():
            print(f"--seed id must be numeric, got {pk!r} for {lab!r}")
            return 1
        seeds[lab] = pk

    if args.apply:
        # Cheap insurance. The whole point of a migration you can re-run is that
        # you can also walk away from it.
        backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")

    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row

    # -- 1. the column -----------------------------------------------------
    if "platform_id" not in _cols(db, "sources"):
        print("sources.platform_id: MISSING -> add")
        if args.apply:
            db.execute("ALTER TABLE sources ADD COLUMN "
                       "platform_id TEXT NOT NULL DEFAULT ''")
            db.commit()
    else:
        print("sources.platform_id: present")

    if "platform_id" not in _cols(db, "sources"):
        # Dry run against the OLD schema. There is no platform_id to query, so
        # everything below is derived from `value` alone — but it must still
        # answer the only question the operator actually has, which is "what is
        # this going to do to my sources". An early return that prints nothing
        # is a dry run that tells you nothing.
        print("\n(dry run — reported against the old schema)")
        rows = [dict(r) for r in db.execute(
            "SELECT label, type, value FROM sources ORDER BY label")]
        moves = [r for r in rows
                 if r["type"] == "user" and r["value"] and r["value"].isdigit()]
        names = [r for r in rows
                 if r["type"] == "user" and r["value"] and not r["value"].isdigit()]
        other = [r for r in rows if r["type"] != "user"]

        print(f"\n{len(rows)} source(s) total")
        if moves:
            print(f"\n{len(moves)} carrying a numeric id in the handle column "
                  f"— the id moves to platform_id, the handle is left empty:")
            for m in moves:
                print(f"  [{m['label']}] {m['value']}")
        else:
            print("\nno numeric values sitting in the handle column — nothing to move")

        if names:
            print(f"\n{len(names)} keyed by handle. The migration does NOT touch "
                  f"these; they keep collecting by name and are resolved to an id "
                  f"on the next pass (or all at once with `collect_ig.py "
                  f"resolve-ids`):")
            for n in names:
                print(f"  [{n['label']}] {n['value']}")
        if other:
            print(f"\n{len(other)} non-user source(s), untouched: "
                  f"{', '.join(repr(o['label']) for o in other)}")

        print("\nNo label is written, in this mode or any other.")
        print("Re-run with --apply to perform the migration.")
        return 0

    # -- 2. numeric handles are ids in the wrong column ---------------------
    moved = 0
    for r in db.execute("SELECT label, value FROM sources "
                        "WHERE type='user' AND platform_id='' AND value != ''"):
        if not r["value"].isdigit():
            continue
        print(f"  move: [{r['label']}] value={r['value']} -> platform_id "
              f"(handle unknown, fill it in later if you want one)")
        moved += 1
        if args.apply:
            db.execute("UPDATE sources SET platform_id=?, value='' WHERE label=?",
                       (r["value"], r["label"]))
    if not moved:
        print("no numeric values sitting in the handle column")

    # -- 3. seeds ----------------------------------------------------------
    for lab, pk in seeds.items():
        row = db.execute("SELECT label, platform_id FROM sources WHERE label=?",
                         (lab,)).fetchone()
        if not row:
            print(f"  seed SKIPPED: no source labelled {lab!r}")
            continue
        if row["platform_id"]:
            print(f"  seed skipped: [{lab}] already has id {row['platform_id']}")
            continue
        print(f"  seed: [{lab}] -> {pk}")
        if args.apply:
            db.execute("UPDATE sources SET platform_id=? WHERE label=?", (pk, lab))

    if args.apply:
        db.commit()

    # -- 4. the worklist ---------------------------------------------------
    pending = [dict(r) for r in db.execute(
        "SELECT label, value FROM sources WHERE type='user' "
        "AND platform_id='' AND value != '' ORDER BY label")]
    print()
    if pending:
        print(f"{len(pending)} source(s) still need a numeric id — these will be "
              f"resolved on the next pass, or all at once with "
              f"`python3 collect_ig.py resolve-ids`:")
        for p in pending:
            print(f"  [{p['label']}] {p['value']}")
        print("\nIf the session is too restricted to look them up, read each id "
              "off view-source:https://www.instagram.com/<handle>/ (search "
              "\"profile_id\") and cache it with:")
        print("  python3 collect_ig.py set-id --label \"<label>\" --id <numeric_id>")
    else:
        print("every user source has a numeric id — nothing left to resolve")

    if not args.apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
