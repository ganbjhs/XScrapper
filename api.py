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
  revoked    INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_used  INTEGER,
  calls      INTEGER NOT NULL DEFAULT 0
);
"""


def _keydb(path=KEY_DB):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(KEY_SCHEMA)
    db.commit()
    return db


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_key(name: str, path=KEY_DB) -> str:
    token = "wt_" + secrets.token_urlsafe(24)
    prefix = token[:11]
    db = _keydb(path)
    db.execute("INSERT INTO api_keys(prefix,key_hash,name,created_at) VALUES(?,?,?,?)",
               (prefix, _hash(token), name, int(time.time())))
    db.commit()
    db.close()
    return token


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

        if self._auth_or_401() is None:
            return

        if path == "/v1/stats":
            with store_ig.Store(self.results_db) as st:
                return self._send(200, st.stats())

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
                    before_pk=one("cursor", int), limit=limit)
            posts = [store_ig.to_api(r) for r in rows]
            next_cursor = posts[-1]["id"] if len(posts) == limit and posts else None
            return self._send(200, {"platform": "instagram", "count": len(posts),
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

    n = sub.add_parser("newkey"); n.add_argument("--name", required=True)
    sub.add_parser("listkeys")
    rv = sub.add_parser("revoke"); rv.add_argument("prefix")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--results-db", default=RESULTS_DB)

    args = ap.parse_args()

    if args.cmd == "newkey":
        token = create_key(args.name)
        print("API key created. This is shown ONCE — copy it now:\n")
        print(f"    {token}\n")
        print(f"Give it to {args.name}. Test it:")
        print(f'    curl -H "Authorization: Bearer {token}" http://127.0.0.1:8790/v1/instagram/posts')
        return 0

    if args.cmd == "listkeys":
        rows = list_keys()
        if not rows:
            print("no keys yet — create one with `api.py newkey --name <who>`")
        for r in rows:
            state = "REVOKED" if r["revoked"] else "active"
            print(f"  {r['prefix']}…  {r['name']:16} {state:8} calls={r['calls']}")
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
