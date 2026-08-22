"""
api.py — the API-key data-extraction service. THIS is the tool's product:
Watch-Tower (or anything else you authorise) calls it with an API key and gets
the collected Instagram content back as clean JSON. Read-only, no analysis —
the caller does sentiment and topics; this serves the raw extract plus the
engagement numbers Instagram already returns.

Two halves:

  KEYS (a CLI you run):
    python3 api.py newkey  --name watch-tower     # prints the key ONCE
    python3 api.py listkeys
    python3 api.py revoke  wt_ab12cd34

  SERVICE (what Watch-Tower calls):
    python3 api.py serve --host 127.0.0.1 --port 8790
      GET /v1/health                      -> no key needed
      GET /v1/stats                       -> key
      GET /v1/instagram/posts?limit=&since=&source=&username=&cursor=  -> key
    Auth:  Authorization: Bearer wt_...   (or ?api_key=wt_...)

SECURITY, deliberately: only the SHA-256 of each key is stored, never the key
itself — a leaked database cannot be used to call the API. The raw key is shown
exactly once, at creation. Bind to 127.0.0.1 and put it behind your normal TLS
proxy, or expose it only to Watch-Tower's server. The key is a bearer token:
whoever holds it can read the extract, so treat it like a password.
"""

import argparse
import hashlib
import json
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import store_ig

VERSION = "1.0"
KEY_DB = "api_keys.db"
RESULTS_DB = "ig_results.db"

KEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  prefix     TEXT PRIMARY KEY,     -- 'wt_' + 8 chars, shown in listings
  key_hash   TEXT NOT NULL UNIQUE, -- sha256 of the full key; the key is never stored
  name       TEXT NOT NULL,
  project_id INTEGER NOT NULL DEFAULT 0,  -- THE SCOPE. A key sees one project's
                                          -- posts and nothing else. 0 = scoped
                                          -- to nothing, and is refused at use.
  revoked    INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_used  INTEGER,
  calls      INTEGER NOT NULL DEFAULT 0
);
"""

# WHY THE KEY CARRIES THE SCOPE, RATHER THAN THE REQUEST
#
# This service is the one surface a THIRD PARTY talks to. If the project were a
# query parameter, scoping would be a request for the caller to make correctly,
# and any caller could ask for any project by changing a number in a URL. That
# is not a boundary, it is a naming convention.
#
# Binding the project to the key inverts it: the caller cannot express "someone
# else's data" at all. There is no parameter to tamper with, the audit question
# ("what can this key see?") is answered by one column, and revoking a key
# revokes exactly one project's access.
#
# A key created before this column existed has project_id 0 and is REFUSED with
# an explanation, not silently served everything. That is deliberate: the old
# behaviour of those keys was "all Instagram data", and quietly keeping it would
# be the exact leak this closes. Re-issue them against a project.


def _keydb(path=KEY_DB):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(KEY_SCHEMA)
    # Self-applying migration, same discipline as the stores (RULEBOOK §7).
    have = {r[1] for r in db.execute("PRAGMA table_info(api_keys)")}
    if "project_id" not in have:
        db.execute("ALTER TABLE api_keys ADD COLUMN "
                   "project_id INTEGER NOT NULL DEFAULT 0")
    db.commit()
    return db


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_key(name: str, path=KEY_DB, project_id: int = 0) -> str:
    """Issue a key FOR ONE PROJECT. A key with project_id 0 authenticates but
    is refused at every data endpoint — there is no such thing as a key that
    reads everything."""
    token = "wt_" + secrets.token_urlsafe(24)
    prefix = token[:11]
    db = _keydb(path)
    db.execute("INSERT INTO api_keys(prefix,key_hash,name,project_id,created_at) "
               "VALUES(?,?,?,?,?)",
               (prefix, _hash(token), name, int(project_id or 0), int(time.time())))
    db.commit()
    db.close()
    return token


def set_key_project(prefix: str, project_id: int, path=KEY_DB) -> bool:
    """Re-scope an existing key — the migration path for keys issued before
    scoping existed."""
    db = _keydb(path)
    cur = db.execute("UPDATE api_keys SET project_id=? WHERE prefix=?",
                     (int(project_id or 0), prefix))
    db.commit()
    n = cur.rowcount
    db.close()
    return n > 0


def verify_key(token: str, path=KEY_DB):
    """Return the key row for a valid, non-revoked token, else None. Bumps usage."""
    if not token or not token.startswith("wt_"):
        return None
    db = _keydb(path)
    row = db.execute("SELECT * FROM api_keys WHERE key_hash=? AND revoked=0",
                     (_hash(token),)).fetchone()
    if row:
        db.execute("UPDATE api_keys SET last_used=?, calls=calls+1 WHERE prefix=?",
                   (int(time.time()), row["prefix"]))
        db.commit()
    db.close()
    return dict(row) if row else None


def list_keys(path=KEY_DB) -> list:
    db = _keydb(path)
    rows = [dict(r) for r in db.execute("SELECT * FROM api_keys ORDER BY created_at")]
    db.close()
    return rows


def revoke_key(prefix: str, path=KEY_DB) -> bool:
    db = _keydb(path)
    cur = db.execute("UPDATE api_keys SET revoked=1 WHERE prefix=?", (prefix,))
    db.commit()
    n = cur.rowcount
    db.close()
    return n > 0


# ==========================================================================
# the HTTP service
# ==========================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = f"CollectorAPI/{VERSION}"
    results_db = RESULTS_DB
    key_db = KEY_DB

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        qs = parse_qs(urlparse(self.path).query)
        return (qs.get("api_key") or [""])[0]

    def _auth_or_401(self):
        key = verify_key(self._token(), self.key_db)
        if not key:
            self._send(401, {"error": "unauthorized",
                             "detail": "supply a valid API key via 'Authorization: Bearer <key>' "
                                       "or ?api_key=<key>"})
            return None
        return key

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        qs = parse_qs(u.query)

        if path == "/v1/health":
            return self._send(200, {"ok": True, "service": "collector-api", "version": VERSION})

        key = self._auth_or_401()
        if key is None:
            return

        # The key IS the scope. A key with no project reads nothing — see the
        # note above KEY_SCHEMA for why this refuses rather than defaults open.
        pid = int(key.get("project_id") or 0)
        if not pid:
            return self._send(403, {
                "error": "key not scoped to a project",
                "detail": ("This API key was issued before project scoping and "
                           "is not bound to a project, so it can read nothing. "
                           "Re-issue it against a project, or re-scope it with "
                           "api.set_key_project(<prefix>, <project_id>)."),
                "key": key.get("prefix")})

        if path == "/v1/stats":
            with store_ig.Store(self.results_db) as st:
                return self._send(200, {**st.stats(project_id=pid),
                                        "project_id": pid})

        if path == "/v1/instagram/posts":
            def one(name, cast=str, default=None):
                v = qs.get(name, [None])[0]
                if v is None:
                    return default
                try:
                    return cast(v)
                except (TypeError, ValueError):
                    return default
            limit = one("limit", int, 50) or 50
            with store_ig.Store(self.results_db) as st:
                rows = st.query(
                    since=one("since", int), until=one("until", int),
                    source=one("source"), username=one("username"),
                    before_pk=one("cursor", int), limit=limit,
                    project_id=pid)
            posts = [store_ig.to_api(r) for r in rows]
            next_cursor = posts[-1]["id"] if len(posts) == limit and posts else None
            return self._send(200, {"platform": "instagram", "project_id": pid,
                                    "count": len(posts),
                                    "next_cursor": next_cursor, "posts": posts})

        return self._send(404, {"error": "not found",
                                "detail": "try /v1/health, /v1/stats, /v1/instagram/posts"})


def serve(host="127.0.0.1", port=8790, results_db=RESULTS_DB, key_db=KEY_DB):
    Handler.results_db = results_db
    Handler.key_db = key_db
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Collector API on http://{host}:{port}  (results={results_db})")
    print("Endpoints: /v1/health  /v1/stats  /v1/instagram/posts")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to a public interface. Keys travel in the clear "
              "without TLS — put this behind an HTTPS proxy.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Collector data-extraction API + key management")
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("newkey")
    n.add_argument("--name", required=True)
    n.add_argument("--project", type=int, required=True,
                   help="the project this key may read. Required: there is no "
                        "key that reads everything.")
    sub.add_parser("listkeys")
    rv = sub.add_parser("revoke"); rv.add_argument("prefix")
    sk = sub.add_parser("scope", help="re-scope an existing key to a project")
    sk.add_argument("prefix")
    sk.add_argument("--project", type=int, required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--results-db", default=RESULTS_DB)

    args = ap.parse_args()

    if args.cmd == "newkey":
        token = create_key(args.name, project_id=args.project)
        print("API key created. This is shown ONCE — copy it now:\n")
        print(f"    {token}\n")
        print(f"Give it to {args.name}. It can read project {args.project} "
              f"and nothing else. Test it:")
        print(f'    curl -H "Authorization: Bearer {token}" http://127.0.0.1:8790/v1/instagram/posts')
        return 0

    if args.cmd == "scope":
        print(f"{args.prefix} -> project {args.project}"
              if set_key_project(args.prefix, args.project)
              else "no key with that prefix")
        return 0

    if args.cmd == "listkeys":
        rows = list_keys()
        if not rows:
            print("no keys yet — create one with `api.py newkey --name <who>`")
        for r in rows:
            state = "REVOKED" if r["revoked"] else "active"
            proj = r["project_id"] or "NONE — reads nothing, re-scope it"
            print(f"  {r['prefix']}…  {r['name']:16} {state:8} "
                  f"project={proj}  calls={r['calls']}")
        return 0

    if args.cmd == "revoke":
        print("revoked" if revoke_key(args.prefix) else "no key with that prefix")
        return 0

    if args.cmd == "serve":
        serve(args.host, args.port, args.results_db)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
