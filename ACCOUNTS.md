# Account Control Panel — Design

The design for a single place to manage the scraper accounts behind **all three
platforms** (X, Instagram, Facebook). Companion to `BLUEPRINT.md` (the map) and
`RULEBOOK.md` (the law). Nothing here is built yet — this is the agreed design,
captured before any code.

> One line: one control panel manages a **pool** of accounts per platform; **one
> account is active** at a time, the rest are warm backups; when the active one
> dies the system **fails over automatically** to a backup (and its own IP);
> login always happens **on the server's own IP** so the session and its IP stay
> coherent; **2FA is cleared by a stored TOTP secret** (Telegram/QR only as a
> fallback). The old single-account Facebook `.env` session is retired.

---

## 1. Why this exists

Today accounts live in three scattered places: X sessions in `accounts.db` (via
the streamed-browser login at `/accounts`), Instagram in cookie/password
sidecars, and Facebook as a single `FB_EMAIL`/`FB_PASSWORD` in `.env` with one
`fb_state.json`. That means three mental models, no shared view of health, and —
for Facebook — a single golden session that, when it dies, takes collection down
until a human hand-fixes it.

Two problems drive this design:

1. **Bans and logouts are normal, not exceptional.** No scraper account is
   ban-proof (`RULEBOOK` §6). The system must treat "the active account died" as
   a routine, automatically-handled event, not a 2am fire.
2. **Per-account setup should be data entry, not engineering.** Onboarding a
   replacement account must be filling a form — never touching code — so adding
   accounts carries no new bugs and no new knowledge burden.

---

## 2. The account model (shared by all three platforms)

Every account — X, IG, or FB — is one row with the **same shape**. Platform
differences live in the engine underneath, never in the model on top.

| Field | Meaning |
|---|---|
| `id` | internal id |
| `platform` | `x` \| `ig` \| `fb` |
| `label` | human name ("fb_backup_2") |
| `login` | username / email |
| `password` | stored encrypted (see §11) |
| `totp_secret` | the authenticator setup key, encrypted; empty if the account has no TOTP |
| `backup_codes` | the platform's one-time recovery codes, encrypted; each marked used once consumed |
| `proxy_id` | which IP/proxy this account is bound to (see §7) |
| `status` | `active` \| `backup` \| `needs_login` \| `quarantined` \| `dead` |
| `health` | last check result + reason |
| `last_success_at` | last successful collection |
| `session_ref` | where the saved browser/session state lives |
| `created_at`, `notes` | housekeeping |

**Status is a small state machine:**

```
        add (form)              promote                fails health
 (new) ─────────────► backup ─────────────► active ─────────────► quarantined
                        ▲                      │                       │
                        │   demote / recover   │  login succeeds       │ re-login
                        └──────────────────────┘◄──────────────────────┘
                                                                        │ unrecoverable
                                                                        ▼
                                                                      dead
```

- **backup** — warm, credentials on file, ready but idle.
- **active** — the one account currently collecting for that platform.
- **needs_login** — session expired; a login attempt is queued.
- **quarantined** — a login/collection failure pulled it out of rotation; it is
  NOT retried in a loop (retrying a flagged account accelerates the ban —
  `FACEBOOK_LESSONS` #4).
- **dead** — banned or unrecoverable; kept for the record, never used.

---

## 3. The control panel (the management surface)

One screen in the dashboard, accounts grouped by platform. It is a full
management surface, not a viewer.

**Per account:**

- **Add** — one form: platform, login, password, TOTP secret (optional),
  proxy/IP. Save → the account enters the pool as `backup`.
- **Remove / retire** — pull a dead or unwanted account out.
- **Edit** — update password, swap proxy/IP, replace the TOTP secret, rename.
- **Refresh backup codes** — paste a fresh set of one-time recovery codes;
  shows **codes remaining**.
- **Log in now** — trigger the server-IP browser login on demand.
- **Promote / demote** — make a backup active, or bench the active one.
- **Status line** — `active/backup/quarantined/dead/needs_login`, health, last
  successful collection, and which IP it is on.

**Per platform (the pool view):**

- Who is active, how many backups remain, and a **low-pool warning** at 1–2 left.
- **Auto-failover** toggle and a **rotate-IP-on-failover** switch.
- **Alerts** surfaced here: session died, backup codes low, SMS/approval needed
  (Tier 3/4 human step), pool low.

The panel is one surface over three **adapters** (X / IG / FB), mirroring the
existing `engine_/collect_/store_` split. Same buttons on top; the right engine
does the platform-specific work below.

---

## 4. Pool + one-active + auto-failover

Per platform: a pool of 2–4 accounts, **exactly one active**, the rest warm
backups. The failover loop:

1. Health check flags the active account (`needs_login` or a hard failure).
2. For a plain expiry → attempt the server-IP re-login (§5) on the same account.
3. If login can't recover it → **quarantine** it, **promote the next backup**,
   and (if enabled) **rotate the new active account's IP** (§7).
4. Collection continues on the promoted account.
5. Alert: "switched to <backup>; N backups left." If the pool hits its last
   account, raise the **low-pool warning** so a human refills it from the form
   when convenient — never under pressure.

The point: a ban costs *one account and one IP* out of a stack you can refill by
typing into a form. It never costs an outage.

---

## 5. Server-IP-coherent login (the durability fix)

The root cause of the Facebook logout storm was **IP mismatch**: cookies minted
on a home IP, then used from the datacenter, read as a hijack — which also kicked
the human's real browser. The fix is a rule:

> **The login browser runs on the server, through the same IP the account will
> collect from. The human never opens the platform on their own IP.**

Login flow:

1. The account needs a session (`needs_login`). The server opens a headless
   browser **bound to that account's proxy/IP** (§7).
2. It submits `login` + `password`.
3. If the platform asks for a 2FA code → §6.
4. On success, the whole session state is saved to `session_ref`; status →
   `active`/`backup`.

Because the login page, the 2FA step, and the resulting session all happen
inside the server's browser on one IP, the cookies (including Facebook's `datr`)
are minted and used from a single coherent IP. One stable browser profile per
account keeps the fingerprint steady across re-logins.

---

## 6. 2FA: a fallback ladder (automatic first, human last)

When a login hits a 2FA/challenge, the server walks a ladder from fully
automatic down to human intervention, and only descends when the tier above
isn't available. Every tier finishes **inside the server browser on the server
IP**, so the session stays IP-coherent no matter which rung is used.

**Tier 1 — TOTP (automatic, unlimited). The default.**
An authenticator app is just a formula: `secret + current time → 6-digit code`.
If the server holds the secret, it generates the identical code the platform
expects.

- One-time, per account, at onboarding: enable 2FA → choose **Authentication
  app** → the platform shows a QR *and* a **setup key** (text). That setup key is
  the secret; paste it into the Add-account form.
- At login the server computes the code itself:

  ```python
  import pyotp
  code = pyotp.TOTP(account.totp_secret).now()   # exactly what the platform wants, now
  ```

- No phone, no relay, never runs out. TOTP is generic, so the **same mechanism
  serves X, IG, and FB** wherever the account uses authenticator 2FA.

**Tier 2 — Backup codes (automatic, but finite).**
When you enable 2FA, the platform also hands you a set of **one-time recovery
codes**. Save them (encrypted) in `backup_codes` at onboarding. If TOTP is
missing or a code is rejected, the server consumes the next unused backup code
automatically — still no human. Because each code works once:

- Mark a code **used** the moment it's spent; never reuse one.
- Track **codes remaining**; raise a **"backup codes low"** alert at ~2 left so
  you regenerate a fresh set (in the account, on a normal browser) and paste them
  back into the panel. A **Refresh backup codes** action holds the new set.
- This is a cushion, not a primary path — it buys automatic recoveries while TOTP
  is the thing that should almost always answer.

**Tier 3 — SMS code (human courier, via Telegram).**
Some accounts only offer SMS/email codes, which the server can't read. Then it
**pings you on Telegram**; you read the code off your phone and reply with the
digits; the server browser types them in. You are only a courier for 6 digits —
the session never touches your IP.

**Tier 4 — Login approval / QR (human action, last resort).**
"Was this you?" / approve-on-device / a login QR can't be automated by design.
The server browser surfaces it (shows the QR, or alerts you) and you **approve
from your phone**. Approving is just "yes it's me"; the **session still lands in
the server browser on the server IP**, so IP coherence holds even here.

> Order of preference: **TOTP → backup codes → SMS relay → login approval.** The
> top two need no human and cover the vast majority of logins; the bottom two are
> the escape hatches for accounts or challenges that force a person into the
> loop. An account stuck waiting on Tier 3/4 sits in `needs_login` and, if it
> can't be cleared promptly, failover promotes a backup so collection never
> stalls.

Keep the same TOTP setup key and a copy of the backup codes in your own
authenticator/records too, as a manual override.

---

## 7. IP / proxy pooling (the honest constraint)

A datacenter server IP can't be made fully "trusted" — the platform distrusts
that whole class. Protection is gentleness (one account active, low volume,
watermark-stop) plus **one account : one steady IP**.

The trap in a backup pool: **if every backup shares the one server IP, they
share its fate.** A ban often means the *IP* got flagged, so promoting a backup
on the same IP just hands the platform its next victim. Therefore:

- Each account is bound to a **proxy/IP** (`proxy_id`).
- **On failover, rotate the IP too** — the promoted account gets a fresh proxy,
  not the poisoned one. Accounts and IPs are pooled and rotated **together**.
- Prefer **steady/sticky** IPs, not rapidly-rotating ones (constant hopping is
  itself a ban signal — `RULEBOOK` §5, "one account, one steady IP").
- Facebook is the case where a **sticky residential IP** buys the most session
  durability; X/IG keep their current IP posture unless a platform says otherwise.

This is a cost line (a proxy per active account). It is the price of durability;
sharing one datacenter IP across the pool quietly kills the backups one by one.

---

## 8. Per-platform adapters (one surface, three engines)

The panel is unified; the mechanics under it stay platform-specific.

- **X** — streamed-browser login → twscrape session in `accounts.db`. Already
  multi-account; the pool/failover model formalizes what X half-does today. TOTP
  slots into the login step.
- **Instagram** — device-pinned cookie/password; checkpoints are hard and
  `PleaseWaitFewMinutes` punishes retries — so quarantine-not-retry matters most
  here. TOTP where the account uses it; otherwise Telegram relay.
- **Facebook** — the new pool + coherent-IP login + TOTP flow, replacing the old
  single session (§9). Collection stays on the current engine (desktop-UA render
  capturing GraphQL); only *account management* changes.

---

## 9. Retiring the old Facebook session

Once Facebook accounts live in the panel:

- Drop the `FB_EMAIL` / `FB_PASSWORD` single-account block from `.env` and the
  single `fb_state.json` golden session.
- That account becomes **one row in the pool** like any other; its session lives
  in `session_ref` alongside the rest.
- No more FB-specific login special-case — FB logs in through the same
  server-IP + TOTP path as X and IG.

Migration is additive (`RULEBOOK` §7): the accounts table is created, the
existing FB credentials are imported into it once, then the old path is removed.
Nothing collected is touched.

---

## 10. Where it plugs into the codebase

- **New store**: an `accounts` table (the §2 model) with additive migrations —
  most naturally a small `store_accounts.py`, or folded into the existing account
  stores behind one interface.
- **web.py**: the control-panel routes (list/add/edit/remove/promote/login-now)
  as thin validators over the store, plus the pool/alert views. Reuses the
  existing `_LOGIN` streamed-browser machinery rather than inventing a new one.
- **frontend/src/views/**: one Accounts view (tokens in `styles.css`, honest
  loading/empty/error states, dark mode) — rebuilt and `dist/` committed.
- **A failover/health tick** in the watch loop: check active accounts, drive the
  state machine, fire alerts.
- **Secrets**: `password` and `totp_secret` encrypted at rest (see §11); the
  accounts DB is git-ignored like every other `*.db`.
- `python3 tests/test_all.py` grows offline tests for the state machine and TOTP
  generation, and stays green.

---

## 11. Security notes

- The TOTP secret and password are **as sensitive as a login** — store them
  encrypted at rest, never in git, never in a log or an API response.
- The accounts DB joins `*.db` and `profiles/` on the git-ignore list.
- The control panel is an operator surface: reachable only behind
  `DASH_USER`/`DASH_PASSWORD`, never via an API key (an API key may read data and
  spend fetch budget, never manage accounts — `RULEBOOK` §5).

---

## 12. Explicitly out of scope

Considered and set aside, on purpose:

- **Phone farm / on-device app automation** — a real phone gives the best IP and
  device trust, but pulling structured data *off* the native app means rooting +
  defeating TLS pinning and device-integrity checks: a fragile treadmill and a
  security-bypass path we are not building. The coherent server-IP browser login
  gets the durability we needed without it.
- **Direct GraphQL "minter" split for Facebook** — parked. One browser that both
  collects and re-logs-in on the server IP is less code and solves the actual
  problem (staying logged in). Revisit only if speed becomes the bottleneck.
- **Fully automating QR "approve-on-device"** — can't be automated by design;
  TOTP is the automation answer, QR stays a manual fallback.

---

## 13. Suggested build order

1. `accounts` table + model + encrypted secrets; import the current FB/IG/X
   credentials into it (additive migration).
2. Server-IP login flow + TOTP generation (reuse the streamed-browser machinery);
   offline tests for the state machine and TOTP.
3. Control-panel API + Accounts view: add / remove / edit / status / login-now.
4. Pool + auto-failover + IP-rotation-on-failover + low-pool alert.
5. Retire the old FB `.env` single-session path.
6. Wire the 2FA ladder end to end: backup-code consumption + low-count alert,
   then the Telegram relay for SMS, then the login-approval surface.

Each step ships behind the current behavior, keeps the test suite green, and
leaves the collector untouched — only *account management* changes.
