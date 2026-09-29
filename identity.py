"""
identity.py — one person, several handles, three platforms.

The problem this solves: the same human is @narendramodi on X, narendramodi
on Instagram and narendramodi on Facebook, and until now each platform's
store knew only its own string. The dashboard showed an Instagram handle
where a name belonged, and "link these" was a button a person had to press
per handle per platform (handle_names).

What this module does instead, on its own, from data the collectors already
hold:

  1. OBSERVE  every handle the collector follows, on every platform, with
              the name and picture the platform itself reported for it —
              X: the author fields on collected tweets (and the X List member
              cache); Instagram: `profiles.full_name` / avatar, filled from
              media rows (never a lookup); Facebook: `page_profiles`.
  2. LINK     handles into persons. Two signals, both from the platforms'
              own data:
                - the SAME handle on two platforms (normalised: case,
                  punctuation) — a politician's handle is a brand and is
                  reused deliberately;                                  0.95
                - the same DISPLAY NAME on two platforms once honorifics,
                  parentheticals and emoji are stripped ("Dr. Prem Chand
                  Bairwa" ≡ "Prem Chand Bairwa (मोदी का परिवार)") — and the
                  handles share a token, or one contains the other.     0.85
                  Name alone, no handle overlap:                        0.70
              Anything weaker stays a separate person. Wrong merges are
              worse than missed ones: a missed one shows two cards, a wrong
              one files one man's posts under another's name.
  3. NAME     each person once: a MANUAL name if an operator set one, else
              the best observed name (longest cleaned name, X first because
              X display names are the most reliably human-written), else
              the handle. Names are never invented.

Manual links and names always win over automatic ones and are never
overwritten by a resolve pass. A wrong automatic merge is undone with
`unlink` (the handle becomes its own person, marked manual so the pass will
not re-merge it).

Storage: two tables in identity.db beside the platform databases —
`persons` and `person_handles`. Its own file, so reading Instagram never
creates or touches results.db (the X store belongs to the X watcher). The
legacy `handle_names` rows in results.db (the old per-handle "Name" button)
are imported once as MANUAL names, so nothing an operator typed is lost.

Cost: bounded by the number of FOLLOWED handles (hundreds), never by the
size of the tweet table — each X handle costs one indexed lookup.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field

CACHE_S = 300          # a resolve is re-run at most this often per process

_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "result": None, "root": None}

# Tokens that say WHO SOMEONE IS TO OTHERS, not who they are: dropped before
# names are compared. Case-insensitive; Hindi honorifics included because
# half the display names here carry one.
HONORIFICS = {
    "ji", "shri", "shree", "sri", "smt", "shrimati", "sh", "dr", "dr.", "adv",
    "adv.", "advocate", "er", "er.", "prof", "prof.", "mla", "mp", "mlc",
    "minister", "hon", "hon.", "official", "team", "office", "mr", "mr.",
    "mrs", "ms", "col", "col.", "gen", "capt", "cm", "pm", "bjp", "inc",
    "sir", "sahab", "saheb", "bhai", "bhaiya", "didi", "mata", "swami",
    "श्री", "श्रीमती", "जी", "डॉ", "डॉ.", "विधायक", "सांसद", "मंत्री",
}
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_SPACES = re.compile(r"\s+")


def norm_name(s) -> str:
    """'Dr. Prem Chand Bairwa (मोदी का परिवार) 🇮🇳' -> 'prem chand bairwa'."""
    s = str(s or "")
    s = _PAREN.sub(" ", s)
    # Keep letters, combining marks (Devanagari vowel signs are marks, not
    # letters — dropping them would turn भजनलाल into भ ज न ल ल) and digits;
    # everything else (emoji, punctuation, symbols) becomes a space.
    s = "".join(ch if unicodedata.category(ch)[0] in ("L", "M", "N") else " " for ch in s)
    toks = [t for t in _SPACES.split(s.strip().lower()) if t and t not in HONORIFICS]
    return " ".join(toks)


def norm_handle(h) -> str:
    """'@Narendra_Modi' -> 'narendramodi'."""
    return re.sub(r"[^a-z0-9]", "", str(h or "").strip().lower().lstrip("@"))


def _handle_tokens(h) -> set:
    """Word-ish pieces of a handle: 'drprembairwa' has none we can split, but
    'prem_chand_bairwa' -> {prem, chand, bairwa}; also every 5+ char run."""
    raw = str(h or "").lower().lstrip("@")
    parts = {p for p in re.split(r"[^a-z0-9]+", raw) if len(p) >= 4}
    return parts


@dataclass
class Obs:
    platform: str          # 'x' | 'ig' | 'fb'
    handle: str            # as followed, lowercased
    name: str = ""         # what the platform reported (display/full name)
    avatar: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.handle}"


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
  person_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  name_source TEXT NOT NULL DEFAULT 'auto',   -- 'auto' | 'manual'
  isolated    INTEGER NOT NULL DEFAULT 0,     -- 1: made by `unlink` — the pass
                                              -- never merges other handles in
  created_ms  INTEGER NOT NULL,
  updated_ms  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS person_handles (
  platform      TEXT NOT NULL,
  handle        TEXT NOT NULL,                -- lowercase, no '@'
  person_id     INTEGER NOT NULL,
  link          TEXT NOT NULL DEFAULT 'auto', -- 'auto' | 'manual'
  confidence    REAL NOT NULL DEFAULT 1.0,
  observed_name TEXT,
  avatar        TEXT,
  updated_ms    INTEGER NOT NULL,
  PRIMARY KEY (platform, handle)
);
CREATE INDEX IF NOT EXISTS ix_person_handles_person ON person_handles(person_id);
"""


def _db_path(root):
    from pathlib import Path
    return Path(root) / "identity.db"


def _con(root):
    con = sqlite3.connect(str(_db_path(root)), timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# observations — what each platform's store knows about the handles we follow
# ---------------------------------------------------------------------------

def observe(root, db_results) -> list:
    out: list = []
    out += _observe_x(db_results)
    out += _observe_ig(root)
    out += _observe_fb(root)
    return out


def _observe_x(db_results) -> list:
    out = []
    from pathlib import Path
    if not Path(str(db_results)).exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db_results}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return out
    try:
        handles: dict = {}
        try:
            for r in con.execute("SELECT DISTINCT handle FROM watchlist_members "
                                 "JOIN watchlists USING(watchlist_id) "
                                 "WHERE kind IN ('query')"):
                handles.setdefault(str(r["handle"]).lower(), Obs("x", str(r["handle"]).lower()))
        except sqlite3.Error:
            pass
        try:
            for r in con.execute("SELECT username, display_name, avatar FROM xlist_members "
                                 "WHERE username != ''"):
                h = str(r["username"]).lower()
                o = handles.setdefault(h, Obs("x", h))
                o.name = o.name or str(r["display_name"] or "")
                o.avatar = o.avatar or str(r["avatar"] or "")
        except sqlite3.Error:
            pass
        # One indexed lookup per handle for the platform's own display name
        # and the newest picture. Bounded by followed handles, not by tweets.
        for h, o in handles.items():
            if o.name and o.avatar:
                continue
            try:
                r = con.execute(
                    "SELECT t.author_display_name AS nm, "
                    "  json_extract(COALESCE(rw.raw_json, t.raw_json), "
                    "               '$.user.profileImageUrl') AS av "
                    "FROM tweets t LEFT JOIN tweet_raw rw USING(tweet_id) "
                    "WHERE t.author_username = ? ORDER BY t.created_ms DESC LIMIT 1",
                    (h,)).fetchone()
            except sqlite3.Error:
                r = None
            if r:
                o.name = o.name or str(r["nm"] or "")
                o.avatar = o.avatar or str(r["av"] or "")
        out = list(handles.values())
    finally:
        con.close()
    return out


def _observe_ig(root) -> list:
    out = []
    p = root / "ig_results.db"
    if not p.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return out
    try:
        have_full = "full_name" in {r["name"] for r in con.execute("PRAGMA table_info(profiles)")}
        fn = "pr.full_name" if have_full else "''"
        for r in con.execute(
                f"SELECT s.value AS handle, s.label, s.platform_id, "
                f"       {fn} AS full_name, pr.avatar_url "
                f"FROM sources s "
                f"LEFT JOIN profiles pr ON (s.platform_id != '' AND pr.user_pk = CAST(s.platform_id AS INTEGER)) "
                f"   OR (s.platform_id = '' AND lower(pr.handle) = lower(s.value)) "
                f"WHERE s.type = 'user' AND s.value != ''"):
            h = str(r["handle"]).lower().lstrip("@")
            out.append(Obs("ig", h, str(r["full_name"] or ""), str(r["avatar_url"] or ""),
                           {"label": r["label"]}))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _observe_fb(root) -> list:
    out = []
    p = root / "fb_results.db"
    if not p.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return out
    try:
        for r in con.execute(
                "SELECT s.label AS handle, pp.display_name, pp.avatar_url "
                "FROM sources s LEFT JOIN page_profiles pp ON pp.handle = lower(s.label)"):
            h = str(r["handle"]).lower().lstrip("@")
            if not h:
                continue
            out.append(Obs("fb", h, str(r["display_name"] or ""), str(r["avatar_url"] or "")))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

class _UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


_PLATFORM_RANK = {"x": 0, "ig": 1, "fb": 2}


def _best_name(obs_list: list) -> str:
    """The name a person is shown under when nobody typed one: the longest
    observed name on the most reliable platform; a handle only as a last
    resort."""
    named = [o for o in obs_list if o.name.strip()]
    if named:
        named.sort(key=lambda o: (_PLATFORM_RANK.get(o.platform, 9), -len(o.name.strip())))
        return named[0].name.strip()
    obs_list = sorted(obs_list, key=lambda o: _PLATFORM_RANK.get(o.platform, 9))
    return obs_list[0].handle if obs_list else ""


def _import_legacy_names(con, db_results) -> None:
    """The old per-handle 'Name' button wrote handle_names in results.db;
    every row becomes a MANUAL person name, merged by exact name. Runs on
    every resolve but is a no-op once every legacy handle is linked."""
    from pathlib import Path
    if not Path(str(db_results)).exists():
        return
    try:
        src = sqlite3.connect(f"file:{db_results}?mode=ro", uri=True, timeout=5)
        src.row_factory = sqlite3.Row
        try:
            rows = src.execute("SELECT platform, handle, display_name FROM handle_names").fetchall()
        finally:
            src.close()
    except sqlite3.Error:
        return
    now = _now_ms()
    for r in rows:
        plat, h, nm = str(r["platform"]), str(r["handle"]).lower(), str(r["display_name"]).strip()
        if not nm:
            continue
        if con.execute("SELECT 1 FROM person_handles WHERE platform=? AND handle=? AND link='manual'",
                       (plat, h)).fetchone():
            continue
        p = con.execute("SELECT person_id FROM persons WHERE name = ? AND name_source='manual'",
                        (nm,)).fetchone()
        if p:
            pid = int(p["person_id"])
        else:
            pid = int(con.execute(
                "INSERT INTO persons(name, name_source, created_ms, updated_ms) "
                "VALUES(?,'manual',?,?)", (nm, now, now)).lastrowid)
        con.execute(
            "INSERT INTO person_handles(platform, handle, person_id, link, confidence, updated_ms) "
            "VALUES(?,?,?,'manual',1.0,?) ON CONFLICT(platform, handle) DO UPDATE SET "
            "person_id = excluded.person_id, link = 'manual', confidence = 1.0, "
            "updated_ms = excluded.updated_ms", (plat, h, pid, now))


def resolve(root, db_results, force: bool = False) -> dict:
    """
    Run (or reuse) a resolve pass. Returns
      {"by_handle": {"x:narendramodi": {...}, ...},
       "persons": {person_id: {"person_id", "name", "name_source", "handles": [...]}}}
    where a by_handle entry is {"person_id", "name", "avatar", "link",
    "confidence", "observed_name"}.
    """
    with _LOCK:
        c = _CACHE
        if (not force and c["result"] is not None and c["root"] == str(root)
                and time.time() - c["at"] < CACHE_S):
            return c["result"]
        result = _resolve_now(root, db_results)
        _CACHE.update({"at": time.time(), "result": result, "root": str(root)})
        return result


def invalidate() -> None:
    with _LOCK:
        _CACHE["result"] = None


def _resolve_now(root, db_results) -> dict:
    obs = observe(root, db_results)
    by_key = {o.key: o for o in obs}
    con = _con(root)
    try:
        _import_legacy_names(con, db_results)
        manual = {f"{r['platform']}:{r['handle']}": int(r["person_id"]) for r in con.execute(
            "SELECT platform, handle, person_id FROM person_handles WHERE link='manual'")}
        isolated = {int(r["person_id"]) for r in con.execute(
            "SELECT person_id FROM persons WHERE isolated = 1")}
        # A manually-linked handle that is no longer followed still needs an
        # observation row so its person keeps a name.
        for k in manual:
            if k not in by_key:
                plat, h = k.split(":", 1)
                by_key[k] = Obs(plat, h)

        uf = _UF()
        for k in by_key:
            uf.find(k)
        conf: dict = {k: 1.0 for k in by_key}

        # 1. manual links: same person_id -> same group; a manual handle is
        #    never merged into ANOTHER manual person automatically (below).
        by_person: dict = {}
        for k, pid in manual.items():
            by_person.setdefault(pid, []).append(k)
        for ks in by_person.values():
            for k in ks[1:]:
                uf.union(ks[0], k)

        def manual_person_of(k):
            r = uf.find(k)
            for kk, pid in manual.items():
                if uf.find(kk) == r:
                    return pid
            return None

        def try_union(a, b, c_):
            """Union a and b unless that would merge two DIFFERENT manual
            persons — a human's decision is not overruled by a heuristic."""
            pa, pb = manual_person_of(a), manual_person_of(b)
            if pa is not None and pb is not None and pa != pb:
                return
            # A person made by `unlink` was split out on purpose: nothing is
            # merged into it, and it is merged into nothing, by heuristics.
            if pa != pb and ((pa in isolated) or (pb in isolated)):
                return
            if uf.find(a) != uf.find(b):
                uf.union(a, b)
                conf[a] = min(conf.get(a, 1.0), c_)
                conf[b] = min(conf.get(b, 1.0), c_)

        # 2. same handle across platforms
        by_nh: dict = {}
        for k, o in by_key.items():
            nh = norm_handle(o.handle)
            if len(nh) >= 4:
                by_nh.setdefault(nh, []).append(k)
        for ks in by_nh.values():
            plats = {by_key[k].platform for k in ks}
            if len(plats) > 1:
                for k in ks[1:]:
                    try_union(ks[0], k, 0.95)

        # 3. same cleaned display name across platforms
        by_nm: dict = {}
        for k, o in by_key.items():
            nn = norm_name(o.name)
            if len(nn) >= 6 and " " in nn:          # two words, not a nickname
                by_nm.setdefault(nn, []).append(k)
        for ks in by_nm.values():
            plats = {by_key[k].platform for k in ks}
            if len(plats) < 2:
                continue
            # Within one platform two handles can share a name (a fan page):
            # merge only ACROSS platforms, one per platform, and only when the
            # name is unique on each platform in this group.
            per_plat: dict = {}
            for k in ks:
                per_plat.setdefault(by_key[k].platform, []).append(k)
            if any(len(v) > 1 for v in per_plat.values()):
                continue
            ks1 = [v[0] for v in per_plat.values()]
            for k in ks1[1:]:
                a, b = by_key[ks1[0]], by_key[k]
                ta, tb = _handle_tokens(a.handle), _handle_tokens(b.handle)
                na, nb = norm_handle(a.handle), norm_handle(b.handle)
                overlap = bool(ta & tb) or (len(na) >= 5 and (na in nb or nb in na))
                try_union(ks1[0], k, 0.85 if overlap else 0.70)

        # 4. groups -> persons
        groups: dict = {}
        for k in by_key:
            groups.setdefault(uf.find(k), []).append(k)
        now = _now_ms()
        person_names = {int(r["person_id"]): (str(r["name"]), str(r["name_source"]))
                        for r in con.execute("SELECT person_id, name, name_source FROM persons")}
        existing_auto = {f"{r['platform']}:{r['handle']}": int(r["person_id"]) for r in con.execute(
            "SELECT platform, handle, person_id FROM person_handles WHERE link='auto'")}
        by_handle: dict = {}
        persons: dict = {}
        for ks in groups.values():
            olist = [by_key[k] for k in ks]
            pid = manual_person_of(ks[0])
            if pid is None:
                # Reuse a previous auto person for stability of ids, if all
                # of the group's previously-linked handles agree on one.
                prev = {existing_auto[k] for k in ks if k in existing_auto}
                pid = prev.pop() if len(prev) == 1 else None
            auto_name = _best_name(olist)
            if pid is None:
                pid = int(con.execute(
                    "INSERT INTO persons(name, name_source, created_ms, updated_ms) "
                    "VALUES(?,'auto',?,?)", (auto_name, now, now)).lastrowid)
                name, src = auto_name, "auto"
            else:
                name, src = person_names.get(pid, (auto_name, "auto"))
                if src != "manual" and name != auto_name and auto_name:
                    con.execute("UPDATE persons SET name=?, updated_ms=? WHERE person_id=?",
                                (auto_name, now, pid))
                    name = auto_name
            for o in olist:
                link = "manual" if o.key in manual else "auto"
                c_ = 1.0 if link == "manual" else conf.get(o.key, 1.0)
                con.execute(
                    "INSERT INTO person_handles(platform, handle, person_id, link, confidence, "
                    "observed_name, avatar, updated_ms) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(platform, handle) DO UPDATE SET "
                    "person_id = CASE WHEN person_handles.link='manual' THEN person_handles.person_id "
                    "                 ELSE excluded.person_id END, "
                    "confidence = CASE WHEN person_handles.link='manual' THEN 1.0 ELSE excluded.confidence END, "
                    "observed_name = excluded.observed_name, avatar = excluded.avatar, "
                    "updated_ms = excluded.updated_ms",
                    (o.platform, o.handle, pid, link, c_, o.name or None, o.avatar or None, now))
                by_handle[o.key] = {"person_id": pid, "name": name, "link": link,
                                    "confidence": round(c_, 2),
                                    "observed_name": o.name or "", "avatar": o.avatar or ""}
            persons[pid] = {"person_id": pid, "name": name, "name_source": src,
                            "handles": [{"platform": o.platform, "handle": o.handle,
                                         "observed_name": o.name, "avatar": o.avatar,
                                         "link": by_handle[o.key]["link"],
                                         "confidence": by_handle[o.key]["confidence"]}
                                        for o in olist]}
        # Persons that lost every handle are dropped (auto only).
        con.execute("DELETE FROM persons WHERE name_source='auto' AND person_id NOT IN "
                    "(SELECT DISTINCT person_id FROM person_handles)")
        con.commit()
    finally:
        con.close()
    return {"by_handle": by_handle, "persons": persons}


# ---------------------------------------------------------------------------
# reads for the API / UI
# ---------------------------------------------------------------------------

def names_map(root, db_results, platform: str) -> dict:
    """{handle: person name} for one platform — the shape the old
    handle_names endpoint returned, so every caller keeps working."""
    plat = {"instagram": "ig", "facebook": "fb", "twitter": "x"}.get(platform, platform)
    res = resolve(root, db_results)
    return {k.split(":", 1)[1]: v["name"] for k, v in res["by_handle"].items()
            if k.startswith(plat + ":")}


def avatar_map(root, db_results, platform: str) -> dict:
    """{handle: best avatar across the person's platforms}."""
    plat = {"instagram": "ig", "facebook": "fb", "twitter": "x"}.get(platform, platform)
    res = resolve(root, db_results)
    best: dict = {}
    for p in res["persons"].values():
        av = ""
        for h in sorted(p["handles"], key=lambda h: _PLATFORM_RANK.get(h["platform"], 9)):
            if h.get("avatar"):
                av = h["avatar"]; break
        for h in p["handles"]:
            if h["platform"] == plat and av:
                best[h["handle"]] = av
    return best


def people(root, db_results) -> list:
    res = resolve(root, db_results)
    out = sorted(res["persons"].values(), key=lambda p: p["name"].lower())
    return out


# ---------------------------------------------------------------------------
# manual corrections
# ---------------------------------------------------------------------------

def rename(root, person_id: int, name: str) -> dict:
    name = str(name or "").strip()
    if not name:
        return {"error": "a person needs a name"}
    con = _con(root)
    try:
        n = con.execute("UPDATE persons SET name=?, name_source='manual', updated_ms=? "
                        "WHERE person_id=?", (name, _now_ms(), int(person_id))).rowcount
        con.commit()
    finally:
        con.close()
    invalidate()
    return {"ok": True} if n else {"error": f"no person {person_id}"}


def link(root, platform: str, handle: str, person_id: int) -> dict:
    """Say by hand that this handle IS that person."""
    h = str(handle or "").strip().lower().lstrip("@")
    if not h:
        return {"error": "handle is required"}
    con = _con(root)
    try:
        if not con.execute("SELECT 1 FROM persons WHERE person_id=?", (int(person_id),)).fetchone():
            return {"error": f"no person {person_id}"}
        con.execute(
            "INSERT INTO person_handles(platform, handle, person_id, link, confidence, updated_ms) "
            "VALUES(?,?,?,'manual',1.0,?) ON CONFLICT(platform, handle) DO UPDATE SET "
            "person_id=excluded.person_id, link='manual', confidence=1.0, updated_ms=excluded.updated_ms",
            (platform, h, int(person_id), _now_ms()))
        con.commit()
    finally:
        con.close()
    invalidate()
    return {"ok": True}


def unlink(root, platform: str, handle: str, name: str = "") -> dict:
    """Split a handle out into its own person (manual, so the pass leaves it)."""
    h = str(handle or "").strip().lower().lstrip("@")
    if not h:
        return {"error": "handle is required"}
    con = _con(root)
    try:
        now = _now_ms()
        r = con.execute("SELECT observed_name FROM person_handles WHERE platform=? AND handle=?",
                        (platform, h)).fetchone()
        nm = str(name or "").strip() or (str(r["observed_name"]) if r and r["observed_name"] else h)
        pid = int(con.execute(
            "INSERT INTO persons(name, name_source, isolated, created_ms, updated_ms) "
            "VALUES(?,?,1,?,?)", (nm, "manual" if name else "auto", now, now)).lastrowid)
        con.execute(
            "INSERT INTO person_handles(platform, handle, person_id, link, confidence, updated_ms) "
            "VALUES(?,?,?,'manual',1.0,?) ON CONFLICT(platform, handle) DO UPDATE SET "
            "person_id=excluded.person_id, link='manual', confidence=1.0, updated_ms=excluded.updated_ms",
            (platform, h, pid, now))
        con.commit()
    finally:
        con.close()
    invalidate()
    return {"ok": True, "person_id": pid}
