"""
store_accounts.py — the unified account pool behind the Account Control Panel.

One store for the scraper accounts of ALL three platforms (X, Instagram,
Facebook). See ACCOUNTS.md for the whole design; this file is the persistence +
state-machine half of it. It deliberately does NOT open a browser, drive a
login, or talk to any platform — that is the engine/adapter layer's job. This
module only remembers accounts, their status, and their secrets, and answers two
questions the failover loop asks constantly: "who is active for platform P?" and
"who do I promote when it dies?".

Design rules it keeps (mirrors store.py):
  * sqlite, WAL, additive self-applying migrations — an old DB upgrades in place.
  * Secrets are ENCRYPTED AT REST, never hashed: a password must be typed back
    and a TOTP secret must generate codes, so both have to be reversible. The
    key is `ACCOUNTS_SECRET_KEY` (any string) from the environment. Storing a
    secret with no key configured is refused, not silently kept in plaintext —
    the same "make the unsafe state impossible" stance the dashboard takes with
    DASH_PASSWORD.
  * One database file (`pool.db`), git-ignored like every other *.db.

Its own DB is separate from twscrape's `accounts.db` on purpose: that file has a
schema twscrape owns and we must not touch. This is our layer, our table.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

PLATFORMS = ("x", "ig", "fb")

# The status state machine (see ACCOUNTS.md §2). Kept as plain strings so the
# dashboard and the DB read the same words a human does.
ACTIVE = "active"          # the one account collecting for its platform
BACKUP = "backup"          # warm, on file, idle, ready to promote
NEEDS_LOGIN = "needs_login"  # session expired; a login attempt is queued
QUARANTINED = "quarantined"  # a failure pulled it out of rotation; NOT retried in a loop
DEAD = "dead"              # banned / unrecoverable; kept for the record, never used

STATUSES = (ACTIVE, BACKUP, NEEDS_LOGIN, QUARANTINED, DEAD)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- One row per scraper account, any platform. The panel is a view over this.
-- Secrets (password, totp_secret, backup_codes) are stored ENCRYPTED; the
-- plain columns below are safe metadata. status is the §2 state machine.
CREATE TABLE IF NOT EXISTS managed_accounts (
  account_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  platform        TEXT NOT NULL,                 -- 'x' | 'ig' | 'fb'
  label           TEXT NOT NULL,                 -- human name ("fb_backup_2")
  login           TEXT NOT NULL,                 -- username / email (metadata)
  enc_password    TEXT NOT NULL DEFAULT '',      -- encrypted
  enc_totp        TEXT NOT NULL DEFAULT '',      -- encrypted authenticator secret; '' = none
  enc_backup_codes TEXT NOT NULL DEFAULT '',     -- encrypted JSON [{code,used}]; '' = none
  proxy_id        TEXT,                          -- which IP/proxy this account is bound to
  status          TEXT NOT NULL DEFAULT 'backup',
  health          TEXT NOT NULL DEFAULT '',      -- last check result + reason
  last_success_at TEXT,                          -- ISO-ms of last successful collection
  session_ref     TEXT,                          -- where the saved browser session lives
  notes           TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE(platform, label)
);

CREATE INDEX IF NOT EXISTS ix_accounts_platform_status
  ON managed_accounts(platform, status);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_ms(ms: int) -> str:
    """ISO-8601 UTC to the millisecond — the same timestamp shape store.py uses."""
    s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000))
    return f"{s}.{ms % 1000:03d}Z"


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------

class SecretError(RuntimeError):
    """Raised when a secret would be stored or read without a key configured."""


class _Cipher:
    """
    Reversible encryption for the three secret columns.

    The key is ANY string in `ACCOUNTS_SECRET_KEY`; we stretch it to a real
    Fernet key with SHA-256 so the operator never has to generate a 32-byte
    base64 blob by hand. No key configured => the cipher is disabled, and any
    attempt to store or read a non-empty secret raises rather than leaking
    plaintext.
    """

    def __init__(self, key_material: str | None):
        self._f = None
        if key_material:
            try:
                from cryptography.fernet import Fernet
            except ImportError as e:  # pragma: no cover - environment issue
                raise SecretError(
                    "ACCOUNTS_SECRET_KEY is set but the 'cryptography' package "
                    "is not installed. `pip install cryptography`."
                ) from e
            digest = hashlib.sha256(key_material.encode("utf-8")).digest()
            self._f = Fernet(base64.urlsafe_b64encode(digest))

    @property
    def enabled(self) -> bool:
        return self._f is not None

    def encrypt(self, plain: str) -> str:
        if not plain:
            return ""
        if not self._f:
            raise SecretError(
                "Refusing to store a secret with no ACCOUNTS_SECRET_KEY set. "
                "Set it in .env; secrets are never kept in plaintext."
            )
        return self._f.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        if not self._f:
            raise SecretError(
                "A stored secret exists but ACCOUNTS_SECRET_KEY is not set, so "
                "it cannot be read. Restore the same key you saved it with."
            )
        return self._f.decrypt(token.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@dataclass
class Account:
    """Safe metadata view of an account — never carries a decrypted secret."""
    account_id: int
    platform: str
    label: str
    login: str
    proxy_id: str | None
    status: str
    health: str
    last_success_at: str | None
    session_ref: str | None
    notes: str
    has_totp: bool
    backup_codes_left: int
    created_at: str
    updated_at: str


class AccountStore:
    """
    Persistence + state machine for the account pool. Synchronous: account
    management is an operator action, not part of the async collection hot loop,
    so it stays plain and easy to test. (Integration can call it from a thread.)
    """

    def __init__(self, path: str = "pool.db", secret_key: str | None = None):
        self.path = path
        # Explicit None means "read the env"; pass "" to force cipher-disabled.
        if secret_key is None:
            secret_key = os.getenv("ACCOUNTS_SECRET_KEY") or None
        self._cipher = _Cipher(secret_key)
        self.db: sqlite3.Connection | None = None

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> "AccountStore":
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        return self

    def _migrate(self) -> None:
        """Add columns that appear after a DB was first created (additive, like
        store.py). Nothing here yet — the table is v1 — but the hook exists so a
        future field is a one-line ALTER, never a wipe."""
        wanted: dict[str, dict[str, str]] = {
            # "managed_accounts": {"new_col": "TEXT"},
        }
        for table, cols in wanted.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        if self.db:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _check_platform(platform: str) -> str:
        p = (platform or "").lower().strip()
        if p not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}, got {platform!r}")
        return p

    @staticmethod
    def _check_status(status: str) -> str:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        return status

    def _row(self, account_id: int) -> sqlite3.Row:
        r = self.db.execute(
            "SELECT * FROM managed_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if r is None:
            raise KeyError(f"no account {account_id}")
        return r

    def _to_account(self, r: sqlite3.Row) -> Account:
        codes_left = 0
        if r["enc_backup_codes"]:
            codes = json.loads(self._cipher.decrypt(r["enc_backup_codes"]))
            codes_left = sum(1 for c in codes if not c["used"])
        return Account(
            account_id=r["account_id"], platform=r["platform"], label=r["label"],
            login=r["login"], proxy_id=r["proxy_id"], status=r["status"],
            health=r["health"], last_success_at=r["last_success_at"],
            session_ref=r["session_ref"], notes=r["notes"],
            has_totp=bool(r["enc_totp"]), backup_codes_left=codes_left,
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def _touch(self, account_id: int) -> None:
        self.db.execute(
            "UPDATE managed_accounts SET updated_at = ? WHERE account_id = ?",
            (_iso_ms(_now_ms()), account_id),
        )

    # -- CRUD ---------------------------------------------------------------

    def add(self, platform: str, label: str, login: str, *, password: str = "",
            totp_secret: str = "", backup_codes: list[str] | None = None,
            proxy_id: str | None = None, notes: str = "") -> int:
        """Onboard an account into the pool. Enters as BACKUP; promote() makes it
        active. Secrets are encrypted here — a non-empty one with no key raises."""
        platform = self._check_platform(platform)
        label = (label or "").strip()
        if not label:
            raise ValueError("label is required")
        now = _iso_ms(_now_ms())
        enc_codes = self._encode_codes(backup_codes) if backup_codes else ""
        cur = self.db.execute(
            "INSERT INTO managed_accounts(platform, label, login, enc_password, "
            "enc_totp, enc_backup_codes, proxy_id, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (platform, label, login, self._cipher.encrypt(password),
             self._cipher.encrypt(_norm_totp(totp_secret)), enc_codes,
             proxy_id, BACKUP, now, now),
        )
        return int(cur.lastrowid)

    def get(self, account_id: int) -> Account:
        return self._to_account(self._row(account_id))

    def list(self, platform: str | None = None) -> list[Account]:
        if platform:
            platform = self._check_platform(platform)
            rows = self.db.execute(
                "SELECT * FROM managed_accounts WHERE platform = ? "
                "ORDER BY status, label", (platform,)
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM managed_accounts ORDER BY platform, status, label")
        return [self._to_account(r) for r in rows]

    def update(self, account_id: int, *, label: str | None = None,
               login: str | None = None, password: str | None = None,
               totp_secret: str | None = None, proxy_id: str | None = None,
               notes: str | None = None) -> None:
        """Edit an account. Only the fields passed are changed; a None leaves the
        column alone (so you can rotate a proxy without re-typing the password)."""
        self._row(account_id)  # existence check
        sets, args = [], []
        if label is not None:
            sets.append("label = ?"); args.append(label.strip())
        if login is not None:
            sets.append("login = ?"); args.append(login)
        if password is not None:
            sets.append("enc_password = ?"); args.append(self._cipher.encrypt(password))
        if totp_secret is not None:
            sets.append("enc_totp = ?"); args.append(self._cipher.encrypt(_norm_totp(totp_secret)))
        if proxy_id is not None:
            sets.append("proxy_id = ?"); args.append(proxy_id)
        if notes is not None:
            sets.append("notes = ?"); args.append(notes)
        if not sets:
            return
        sets.append("updated_at = ?"); args.append(_iso_ms(_now_ms()))
        args.append(account_id)
        self.db.execute(
            f"UPDATE managed_accounts SET {', '.join(sets)} WHERE account_id = ?", args)

    def remove(self, account_id: int) -> None:
        self._row(account_id)
        self.db.execute("DELETE FROM managed_accounts WHERE account_id = ?", (account_id,))

    # -- status machine -----------------------------------------------------

    def set_status(self, account_id: int, status: str, health: str = "") -> None:
        status = self._check_status(status)
        self.db.execute(
            "UPDATE managed_accounts SET status = ?, health = ?, updated_at = ? "
            "WHERE account_id = ?",
            (status, health, _iso_ms(_now_ms()), account_id),
        )

    def active(self, platform: str) -> Account | None:
        """The one active account for a platform (newest if somehow >1)."""
        platform = self._check_platform(platform)
        r = self.db.execute(
            "SELECT * FROM managed_accounts WHERE platform = ? AND status = ? "
            "ORDER BY updated_at DESC LIMIT 1", (platform, ACTIVE)).fetchone()
        return self._to_account(r) if r else None

    def next_backup(self, platform: str) -> Account | None:
        """The backup that would be promoted next: oldest-idle first (round-robin
        by updated_at) so accounts get rested evenly rather than one being hammered."""
        platform = self._check_platform(platform)
        r = self.db.execute(
            "SELECT * FROM managed_accounts WHERE platform = ? AND status = ? "
            "ORDER BY updated_at ASC LIMIT 1", (platform, BACKUP)).fetchone()
        return self._to_account(r) if r else None

    def backups_left(self, platform: str) -> int:
        platform = self._check_platform(platform)
        return self.db.execute(
            "SELECT COUNT(*) FROM managed_accounts WHERE platform = ? AND status = ?",
            (platform, BACKUP)).fetchone()[0]

    def promote(self, account_id: int) -> None:
        """Make this account the active one for its platform; any current active
        account is demoted to backup. Enforces the one-active invariant in code
        (sqlite can't express it as a constraint)."""
        r = self._row(account_id)
        platform = r["platform"]
        now = _iso_ms(_now_ms())
        # Demote whoever is active now (but not this account).
        self.db.execute(
            "UPDATE managed_accounts SET status = ?, updated_at = ? "
            "WHERE platform = ? AND status = ? AND account_id != ?",
            (BACKUP, now, platform, ACTIVE, account_id),
        )
        self.db.execute(
            "UPDATE managed_accounts SET status = ?, updated_at = ? WHERE account_id = ?",
            (ACTIVE, now, account_id),
        )

    def quarantine(self, account_id: int, reason: str = "") -> None:
        """Pull an account out of rotation after a failure. It is NOT retried in
        a loop — retrying a flagged account only speeds the ban (FACEBOOK_LESSONS
        #4). It waits here until a human clears and re-promotes it, or retires it."""
        self.set_status(account_id, QUARANTINED, reason or "quarantined after failure")

    def mark_dead(self, account_id: int, reason: str = "") -> None:
        self.set_status(account_id, DEAD, reason or "banned / unrecoverable")

    def record_success(self, account_id: int) -> None:
        """Collection worked: stamp last_success_at and clear any health note."""
        self.db.execute(
            "UPDATE managed_accounts SET last_success_at = ?, health = '', updated_at = ? "
            "WHERE account_id = ?",
            (_iso_ms(_now_ms()),) * 2 + (account_id,),
        )

    def failover(self, platform: str, *, rotate_proxy: str | None = None,
                 reason: str = "") -> Account | None:
        """The move the health loop makes when the active account dies:
        quarantine the active one, promote the next backup, and (per ACCOUNTS.md
        §7) optionally bind the promoted account to a FRESH proxy — because a ban
        often means the IP was flagged, and promoting onto the same IP just feeds
        the platform its next victim. Returns the new active account, or None if
        the pool is empty."""
        platform = self._check_platform(platform)
        cur = self.active(platform)
        if cur:
            self.quarantine(cur.account_id, reason or "failover: active account failed")
        nxt = self.next_backup(platform)
        if not nxt:
            return None
        if rotate_proxy is not None:
            self.update(nxt.account_id, proxy_id=rotate_proxy)
        self.promote(nxt.account_id)
        return self.get(nxt.account_id)

    # -- secrets / 2FA ------------------------------------------------------

    def get_password(self, account_id: int) -> str:
        return self._cipher.decrypt(self._row(account_id)["enc_password"])

    def totp_now(self, account_id: int) -> str | None:
        """Tier 1 of the 2FA ladder: generate the current authenticator code from
        the stored secret. Returns None if this account has no TOTP secret."""
        secret = self._cipher.decrypt(self._row(account_id)["enc_totp"])
        if not secret:
            return None
        import pyotp
        return pyotp.TOTP(secret).now()

    def set_backup_codes(self, account_id: int, codes: list[str]) -> None:
        """Tier 2: store (replace) the one-time recovery codes. Call this at
        onboarding and again from 'Refresh backup codes' when the set runs low."""
        self._row(account_id)
        self.db.execute(
            "UPDATE managed_accounts SET enc_backup_codes = ?, updated_at = ? "
            "WHERE account_id = ?",
            (self._encode_codes(codes), _iso_ms(_now_ms()), account_id),
        )

    def take_backup_code(self, account_id: int) -> str | None:
        """Consume the next unused backup code and mark it used (once only).
        Returns None when none remain — the caller then drops to Tier 3 (SMS
        relay) and the low-count alert should already have fired."""
        r = self._row(account_id)
        if not r["enc_backup_codes"]:
            return None
        codes = json.loads(self._cipher.decrypt(r["enc_backup_codes"]))
        for c in codes:
            if not c["used"]:
                c["used"] = True
                self.db.execute(
                    "UPDATE managed_accounts SET enc_backup_codes = ?, updated_at = ? "
                    "WHERE account_id = ?",
                    (self._cipher.encrypt(json.dumps(codes)), _iso_ms(_now_ms()), account_id),
                )
                return c["code"]
        return None

    def backup_codes_remaining(self, account_id: int) -> int:
        return self.get(account_id).backup_codes_left

    def _encode_codes(self, codes: list[str]) -> str:
        clean = [{"code": str(c).strip(), "used": False} for c in codes if str(c).strip()]
        return self._cipher.encrypt(json.dumps(clean)) if clean else ""


def _norm_totp(secret: str) -> str:
    """Users paste the setup key with spaces and mixed case ('abcd efgh ...').
    Base32 wants no spaces; normalize so pyotp accepts it."""
    return (secret or "").replace(" ", "").strip().upper()
