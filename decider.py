"""
decider.py — ONE place that decides what a collector does when something
happens, instead of every call site printing a line and carrying on.

The problem it was written for (2026-09-03). The Instagram service woke up
every twenty-odd minutes, found no enabled source, wrote

    no enabled sources — add one with `collect_ig.py add-source`

to the Activity Log, and went back to sleep. Twenty-eight identical rows in a
day. Nothing decided anything: the collector did not slow down (nothing to
collect, same cadence), nobody was told (the line is a warn among warns), and
a real error arriving in the same column would have scrolled off the page.
The same shape sits under every other condition the collector meets: a
session it cannot load is printed once per pass forever; a checkpoint is
caught by a generic `except Exception`, logged as "error", and the pass moves
on to the next source with the same account — which is exactly how a
clearable checkpoint becomes a dead account (RULEBOOK §6, Instagram).

So: a condition is an EVENT, an event goes through a POLICY, and the policy
returns a DECISION — what to do, how long to wait, whether this is news, and
whether a human needs to hear about it. Rule-based, deterministic, offline
testable. It is plumbing over the collector's own outcomes, not analysis of
anything collected (RULEBOOK §1 directive 2 is untouched).

The four things a decision carries:

    action    collect | idle | backoff | relogin | quarantine | rest
    wait_s    how long the loop should sleep before trying this again
    say       ONE line for the Activity Log — only when something changed
    notify    text for the operator (Telegram) — only when it is time

"Only when something changed" is the whole point. A condition is logged when
it OPENS, reminded about every REMIND_EVERY while it persists, escalated to
the operator ONCE when it has lasted longer than the rule allows, and logged
again when it CLOSES ("recovered after 3h 12m"). The 28 rows become 3. The
state that makes this possible lives in a small table in activity.db, so a
service restart does not re-announce an open condition and the escalation
clock survives `systemctl restart`.

What each rule decides, and why — this table IS the decider:

    paused           idle 60s, say once, never escalate. The operator pressed
                     Pause; telling them about it would be noise.
    no_sources       idle 30m, say once, remind every 6h, tell the operator
                     after 2h. Nothing to collect is not urgent, but a project
                     that has stayed empty across a working morning is a
                     project someone forgot to set up.
    session_missing  idle 30m, tell the operator at once. No saved session
                     means no collection until a human signs in.
    session_rejected relogin ONCE this pass (the caller does it — see
                     collect_ig), then if it still fails: idle 1h and tell
                     the operator. A rejected cookie is usually a sign-in
                     away from fixed; a second knock is not.
    checkpoint       quarantine: pull the account out of rotation
                     (pool_link.quarantine), stop the pass for this account,
                     back off 6h, tell the operator at once. Only a human can
                     clear a checkpoint, and every extra request while it is
                     open makes it worse.
    rate_limited     back off 15m, doubling to 4h, and stop the pass for this
                     account; tell the operator after it has lasted 6h.
                     PleaseWaitFewMinutes punishes retries — the only correct
                     response is to wait longer each time.
    budget_spent     rest until tomorrow (the caller knows when tomorrow is;
                     the decision says 1h and the loop re-asks). Say once.
    proxy_broken     the request never reached Instagram (TLS verification
                     failed = the proxy exit intercepts HTTPS; or the
                     connection died first). Stop the pass for this account,
                     back off 30m doubling to 4h, tell the operator at once,
                     and fold any per-handle "needs its id" cards into it —
                     they were all this. Only a proxy change fixes it.
    pass_error       back off 10m, doubling to 1h; tell the operator on the
                     third consecutive occurrence. One exception is weather;
                     three in a row is a broken engine.
    ok               collect, clear the open condition for this scope, and
                     say "recovered" if one was open.

Scope. A condition belongs to (platform, account); platform-wide conditions
(no_sources, paused, pass_error) use an empty account. One scope holds ONE
open condition at a time: a pass that produces a different outcome closes
the old one and says so.

Telegram. The operator is paged through the ADMIN BOT — its own bot,
ADMIN_TELEGRAM_BOT_TOKEN, to ADMIN_TELEGRAM_CHAT_ID (falling back to the
delivery bot / chat so an older .env still pages someone; `python3
decider.py test` proves the channel, `chat` finds the id). No token means the
decision still logs and escalates, it just says (once) that it has nowhere
to send. Sending never raises and never blocks collection for more than a
few seconds.

A ping is SHORT (2026-09-06): the condition in one line, "Do:" — the first
move — in one line, "Now:" — what the collector already did — when there is
one, and the links. The long steps live on the Fix panel the link opens. And
a ping is RARE: once when the condition becomes due; the same condition on
the same scope not again for REPING_COOLDOWN_S however often it closes and
re-opens (a checkpoint that re-opens after every sign-in attempt is one
story); one "still open" at STILL_OPEN_AFTER_S for a condition a human must
clear, then silence until it closes; "recovered" only for a story the phone
heard the start of. A one-shot decider (Fetch-now, the CLI) never pages —
the reason is on the screen in front of the operator, and the service loop
pages once if the condition persists.

The pager (Phase 1, 2026-09-03). A ping is only useful if it ends in a fix,
so every ping carries a LINK: `PUBLIC_BASE_URL/app/accounts?fix=<condition
id>` opens the dashboard's Fix panel for exactly that condition — what
happened, the steps, the sign-in box, the re-enable button — and a second
link snoozes it for six hours. A condition id is `platform:account:kind`
(`instagram:sanaakhtar221:checkpoint`; `-` for a platform-wide account). The
same table backs the panel: `open_conditions()` lists what is open with its
steps and available actions, `snooze()` silences one, `resolve()` closes one
by hand. A condition that was pinged and then closes — by itself or by you —
pings once more ("recovered"), so the phone always sees the end of a story it
saw the start of. Rules carry `fix` (the human steps) and `actions` (what
the panel may offer), so the panel never invents advice the policy did not.
"""

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

DEFAULT_DB = os.getenv("ACTIVITY_LOG_DB", "activity.db")
_LOCK = threading.Lock()

H = 3600
REMIND_EVERY_S = 6 * H          # reminder LINES in the Activity Log

# The phone (2026-09-06). A condition pages ONCE when it becomes due. The same
# condition on the same scope pages again only after REPING_COOLDOWN_S, however
# many times it closes and re-opens in between — a checkpoint that re-opens
# after every sign-in attempt is one story, not five. A condition a human
# must clear gets ONE "still open" ping at STILL_OPEN_AFTER_S, then silence
# until it is closed. "Recovered" goes out only for a condition the phone
# heard about; a re-open that was deliberately not paged twice earns one only
# if it was open long enough to have been a story (RECOVERED_MIN_OPEN_S), and
# never twice within RECOVERED_DEDUPE_S for the same scope.
REPING_COOLDOWN_S = 24 * H
STILL_OPEN_AFTER_S = 24 * H
RECOVERED_MIN_OPEN_S = 10 * 60
RECOVERED_DEDUPE_S = 1 * H

# ---------------------------------------------------------------- the rules

@dataclass(frozen=True)
class Rule:
    action: str                 # what the collector should do next
    wait_s: int                 # base sleep before the next attempt
    max_wait_s: int = 0         # for backoff rules: the ceiling (0 = fixed)
    escalate_after_s: int = -1  # -1 never; 0 at once; N after N seconds open
    escalate_after_n: int = 0   # or after this many occurrences (0 = off)
    level: str = "warn"         # activity-log level for the opening line
    title: str = ""             # the ping's first line, after "IG @acct —".
                                # Empty = this rule never pages.
    short: str = ""             # the ping's "Do:" line — the FIRST move, in
                                # one line. The long steps stay on the panel.
    remind_every_s: int = REMIND_EVERY_S
    fix: tuple = ()             # the steps the Fix panel shows, in order
    actions: tuple = ()         # panel buttons: signin | add_source |
                                #   reenable_sources | resolve | resume | set_id
    needs_human: bool = False   # can code close this on its own? no → True
    loop_wait: bool = True      # does wait_s hold the whole LOOP (an account
                                # or platform condition) or just this scope
                                # (a per-source hold-off)? see Decider.holdoff


RULES = {
    "ok": Rule("collect", 0, level="info"),
    "paused": Rule(
        "idle", 60, level="info",
        # no title: the operator did this; never page
        fix=("Collection is paused from the dashboard.",
             "Resume it in Watchlists → Network & settings when you are ready."),
        actions=("resume",),
    ),
    "no_sources": Rule(
        "idle", 30 * 60, escalate_after_s=2 * H, level="warn",
        title="nothing to collect for {open_for} (no enabled source)",
        short="Watchlists → + New watchlist → Instagram, or re-enable one",
        fix=("No Instagram source is enabled in any project, so the collector "
             "has nothing to do.",
             "Add a source: Watchlists → + New watchlist → Instagram, or "
             "re-enable the ones that were switched off."),
        actions=("add_source", "reenable_sources"),
        needs_human=True,
    ),
    "session_missing": Rule(
        "idle", 30 * 60, escalate_after_s=0, level="error",
        title="no working session — nothing collected until signed in",
        short="Accounts & Sessions → @{account} → Sign in",
        fix=("There is no working saved session for this account.",
             "Sign in below — paste the cookies from your own browser (safest) "
             "or run the background sign-in with the stored password."),
        actions=("signin", "resolve"),
        needs_human=True,
    ),
    "session_rejected": Rule(
        "idle", 1 * H, escalate_after_s=0, level="error",
        title="session rejected; one re-login failed",
        short="Accounts & Sessions → @{account} → Sign in with a fresh session",
        fix=("Instagram rejected the session and one automatic re-login did "
             "not fix it (a second attempt is never made — it earns a "
             "checkpoint).",
             "Sign in below with a fresh session from your browser."),
        actions=("signin", "resolve"),
        needs_human=True,
    ),
    "checkpoint": Rule(
        "quarantine", 6 * H, escalate_after_s=0, level="error",
        title="CHECKPOINT — out of rotation, nothing knocks on it",
        short="Accounts & Sessions → @{account} → Sign in → Open this "
              "account's browser → clear \"confirm it's you\"",
        fix=("Instagram is asking a human to confirm it's really @{account}. "
             "No code can answer that, and every automatic retry makes it "
             "worse, so the account is out of rotation.",
             "1. On a trusted phone or browser, log in to Instagram as "
             "@{account} and complete the \"confirm it's you\" check.",
             "2. From that SAME browser, copy the cookies (sessionid at "
             "least) and import them below.",
             "3. Re-enable the sources that were switched off, and the "
             "collector picks the account back up on its next pass."),
        actions=("signin", "reenable_sources", "resolve"),
        needs_human=True,
    ),
    "rate_limited": Rule(
        "backoff", 15 * 60, max_wait_s=4 * H, escalate_after_s=6 * H,
        level="warn",
        title="rate-limited for {open_for} (backing off up to 4h)",
        short="nothing yet; if it lasts a day, lower IG_DAILY_BUDGET or "
              "rest the account",
        fix=("Instagram asked this account to slow down. The collector is "
             "already backing off (15m, doubling to 4h) and will clear this "
             "by itself.",
             "Only if it has lasted most of a day: lower IG_DAILY_BUDGET in "
             ".env or leave the account resting until tomorrow."),
        actions=("resolve",),
    ),
    "budget_spent": Rule(
        "rest", 1 * H, level="info",
        # no title: by design; a person doesn't open the app 500×/day
    ),
    "lookup_throttled": Rule(
        # ONE condition per ACCOUNT (scope account/lookups), not one per
        # handle. A 429 or a login-bounce on the lookup endpoints is the
        # SESSION's state: the next handle gets the same answer, and asking
        # extends the throttle. So the first refusal in a pass stops every
        # further lookup on that account, the sources that already have ids
        # keep collecting, and the account is probed again once — after 6h,
        # then 12h, then daily. The admin is told on the second consecutive
        # refusal; after that it is one request a day until Instagram
        # relents, a proxy is set, or the ids are pasted.
        "hold", 6 * H, max_wait_s=24 * H, escalate_after_n=2, level="warn",
        loop_wait=False,
        title="name lookups refused {count}× ({detail}) — probed once a day now",
        short="Edit @{account} → set its residential proxy; or paste ids on "
              "the Fix panel",
        fix=("Instagram is refusing to translate handles into ids from this "
             "account's session — a throttle (429) or a login-bounce, which "
             "is what a datacenter IP gets. Every extra attempt makes it "
             "last longer, so the collector has stopped asking: at most one "
             "probe a day from here on.",
             "Fix the cause: on this account's card, set its residential "
             "proxy URL (Edit → proxy). Lookups from a residential IP are "
             "the path that works on restricted sessions.",
             "Fix the symptom for a handle you need now: open "
             "https://www.instagram.com/<handle>/ in a browser, view source, "
             "search \"profile_id\", and Save id on that handle's own card "
             "(they appear below when a handle is refused on its own)."),
        actions=("retry", "resolve"),
        needs_human=True,
    ),
    "unresolved_source": Rule(
        "skip", 1 * H, max_wait_s=24 * H, escalate_after_n=3, level="warn",
        loop_wait=False,
        title="'{detail}' has no numeric id ({count} tries); the rest collect",
        short="paste the id on the Fix panel, or follow @{detail} from @{account}",
        fix=("Instagram refused to translate this handle into its numeric "
             "id. Name lookup is a separate permission from reading posts, "
             "and a restricted session loses it first — so a cleaner account "
             "may resolve it by itself.",
             "To fix it by hand, once: open https://www.instagram.com/{detail}/ "
             "in a browser, view the page source, search for \"profile_id\" "
             "and paste the number below. The label and the handle stay as "
             "they are."),
        actions=("set_id", "resolve"),
        needs_human=True,
    ),
    "proxy_broken": Rule(
        # ONE condition per ACCOUNT. The request never reached Instagram —
        # the TLS handshake failed verification (a Sophos/ISP firewall on
        # the proxy's residential exit re-signing HTTPS) or the connection
        # died before any reply. This is not a handle problem, not a throttle
        # and not a checkpoint, and it is the same for EVERY request from the
        # account (lookups AND post reads), so: stop the pass on this account
        # at once, fold any per-handle "needs its id" cards into this one,
        # tell the operator now, and re-probe with one request every 30m,
        # doubling to 4h. Nothing here self-heals; a person changes the
        # proxy exit. (Discovered 2026-09-03: eight "handle needs its numeric
        # id" cards on @shoaibakhtar4915 that were one Sophos MITM on
        # webshare session IN-32.)
        "backoff", 30 * 60, max_wait_s=4 * H, escalate_after_s=0,
        level="error",
        title="nothing reaches Instagram — {detail}",
        short="Edit @{account} → proxy: a different exit, then Retry",
        fix=("Every request from @{account} dies on the way to Instagram, "
             "before Instagram answers: {detail}. So this is NOT a handle "
             "problem, not a throttle and not a checkpoint — it is the "
             "proxy. Pasting numeric ids would not help; post reads leave "
             "through the same pipe.",
             "Test it on the server exactly as the collector does: "
             "curl -x '<this account's proxy URL>' -sS -o /dev/null "
             "-w '%{{http_code}}\\n' https://www.instagram.com/ — you want "
             "200 or 302.",
             "\"SSL certificate problem: unable to get local issuer "
             "certificate\" = that proxy EXIT intercepts HTTPS. Confirm with "
             "the same curl plus -k -v and read the issuer: line (a Sophos / "
             "ISP firewall signs it instead of DigiCert). Connection refused "
             "or 407 = the endpoint or its credentials are dead.",
             "Fix: Accounts & Sessions → @{account} → Edit → proxy: a "
             "different exit (change the session number in the proxy "
             "username, e.g. -IN-32 → -IN-35), re-run the curl until it "
             "passes without -k, then click Retry. The handles resolve by "
             "themselves on the next pass."),
        actions=("retry", "resolve"),
        needs_human=True,
    ),
    "pass_error": Rule(
        "backoff", 10 * 60, max_wait_s=1 * H, escalate_after_n=3,
        level="error",
        title="collector failed {count} passes in a row ({detail})",
        short="on the server: journalctl -u xscraper-ig -n 100",
        fix=("The whole collection pass is crashing, which is a code or "
             "server problem rather than an Instagram one.",
             "On the server: journalctl -u xscraper-ig -n 100 — the traceback "
             "is there. Mark this fixed once the service is passing again."),
        actions=("resolve",),
        needs_human=True,
    ),
}

# instagrapi exception class names → event kind. Matched by NAME so this
# module (and the test suite) never needs instagrapi importable.
_EXC_KIND = {
    "ChallengeRequired": "checkpoint",
    "ChallengeError": "checkpoint",
    "ChallengeSelfieCaptcha": "checkpoint",
    "ChallengeUnknownStep": "checkpoint",
    "SelectContactPointRecoveryForm": "checkpoint",
    "RecaptchaChallengeForm": "checkpoint",
    "FeedbackRequired": "checkpoint",
    "PleaseWaitFewMinutes": "rate_limited",
    "RateLimitError": "rate_limited",
    "ClientThrottledError": "rate_limited",
    "LoginRequired": "session_rejected",
    "ClientLoginRequired": "session_rejected",
    # The request never reached Instagram (see RULES['proxy_broken']).
    # instagrapi wraps requests.ConnectionError — which includes SSLError and
    # ProxyError — as ClientConnectionError; the bare names cover the web
    # paths in engine_ig that use requests directly.
    "ClientConnectionError": "proxy_broken",
    "SSLError": "proxy_broken",
    "SSLCertVerificationError": "proxy_broken",
    "ProxyError": "proxy_broken",
    "ConnectionError": "proxy_broken",
    "ConnectTimeout": "proxy_broken",
    "NewConnectionError": "proxy_broken",
    "MaxRetryError": "proxy_broken",
}


def classify_exception(e: BaseException) -> str:
    """Which event an exception from the engine is. Unknown → pass_error.

    A RuntimeError from ig_session that mentions a checkpoint is a checkpoint
    too — ig_session.refresh wraps ChallengeRequired in RuntimeError with the
    operator help text, and that must not be filed under 'pass_error'.
    """
    name = type(e).__name__
    if name in _EXC_KIND:
        return _EXC_KIND[name]
    text = str(e).lower()
    if "could not resolve the username" in text:
        return "unresolved_source"
    if "checkpoint" in text or "challenge" in text:
        return "checkpoint"
    if "please wait" in text or "rate limit" in text:
        return "rate_limited"
    if "login_required" in text or "login required" in text:
        return "session_rejected"
    if "too many 429" in text:
        # requests.RetryError after urllib3 re-sent a 429 three times: an
        # answer from Instagram, not a dead pipe.
        return "rate_limited"
    if ("certificate verify failed" in text or "unable to get local issuer" in text
            or "caused by newconnectionerror" in text or "caused by proxyerror" in text
            or "caused by connecttimeouterror" in text or "connection refused" in text
            or "tunnel connection failed" in text or "cannot connect to proxy" in text):
        return "proxy_broken"
    return "pass_error"


# ------------------------------------------------------------- the objects

@dataclass
class Event:
    kind: str
    platform: str = "instagram"
    account: str = ""
    detail: str = ""
    source: str = ""            # a per-SOURCE condition (unresolved id) —
                                # keyed as account/source so two sources on
                                # one account do not overwrite each other

    @property
    def who(self) -> str:
        return _who(self.account, self.source)

    @property
    def scope(self) -> str:
        return f"{self.platform}:{self.who}"


def _who(account: str, source: str = "") -> str:
    """The account/source part of a scope. ':' is the id separator, so a
    label may not carry one."""
    a = account or "-"
    return f"{a}/{source.replace(':', '·')}" if source else a


def _split_who(who: str):
    acct, _, src = (who or "-").partition("/")
    return ("" if acct == "-" else acct), src


@dataclass
class Decision:
    action: str
    wait_s: int
    reason: str
    say: list = field(default_factory=list)    # activity-log lines (0..n)
    notify: str = ""                           # operator text ("" = none)
    kind: str = ""
    count: int = 1
    open_since_ms: int = 0
    cond_id: str = ""                          # platform:account:kind

    @property
    def stop_account(self) -> bool:
        """Should the pass stop touching this account right now?

        Quarantine, rest and a rate limit all mean "one more request makes it
        worse"; a broken proxy means "one more request is the same dead
        pipe"; a generic pass_error is neither, so the next source still runs.
        """
        return (self.action in ("quarantine", "relogin", "rest")
                or self.kind in ("rate_limited", "proxy_broken"))


def _fmt_dur(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < H:
        return f"{s // 60}m"
    return f"{s // H}h {(s % H) // 60:02d}m"


def cond_id(platform: str, account: str, kind: str, source: str = "") -> str:
    return f"{platform}:{_who(account, source)}:{kind}"


def parse_cond_id(cid: str):
    """'instagram:sanaakhtar221:checkpoint' → (platform, account, kind).
    'instagram:sana/Bhajanlal Sharma:unresolved_source' keeps the source in
    the account part (see _split_who). Account '-' means platform-wide."""
    parts = (cid or "").split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"bad condition id {cid!r}")
    plat, who, kind = parts
    if kind not in RULES:
        raise ValueError(f"unknown condition kind {kind!r}")
    return plat, who, kind


def base_url() -> str:
    """Where the dashboard lives, for links in pings. PUBLIC_BASE_URL is the
    same variable delivery uses for media links (.env.example)."""
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def fix_url(cid: str, *, snooze_h: int = 0) -> str:
    base = base_url()
    if not base:
        return ""
    u = f"{base}/app/accounts?fix={cid}"
    return f"{u}&snooze={snooze_h}" if snooze_h else u


# --------------------------------------------------------------- the state

class _State:
    """Open conditions, one per scope, in activity.db (or in memory).

    Columns: scope, kind, first_ms (opened), last_ms (last seen), count,
    reminded_ms (last reminder line), notified_ms (0 = operator not yet told),
    detail (latest). `db=None` keeps everything in a dict — the dashboard's
    Fetch-now button uses that, so a one-shot run always speaks.
    """

    def __init__(self, db=DEFAULT_DB):
        self.db = db
        self.mem = {}
        self.mem_pinged = {}        # (scope, kind) -> ms, in-memory mode

    def _con(self):
        con = sqlite3.connect(self.db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")   # shared with activity_log
        except sqlite3.OperationalError:
            pass
        con.execute(
            "CREATE TABLE IF NOT EXISTS decider_state ("
            "  scope       TEXT PRIMARY KEY,"
            "  kind        TEXT NOT NULL,"
            "  first_ms    INTEGER NOT NULL,"
            "  last_ms     INTEGER NOT NULL,"
            "  count       INTEGER NOT NULL DEFAULT 1,"
            "  reminded_ms INTEGER NOT NULL DEFAULT 0,"
            "  notified_ms INTEGER NOT NULL DEFAULT 0,"
            "  detail      TEXT,"
            "  snoozed_until_ms INTEGER NOT NULL DEFAULT 0,"
            "  meta        TEXT)")
        # Additive, self-applying (BLUEPRINT invariant 8): a table created by
        # the first version lacks the two pager columns.
        have = {r[1] for r in con.execute("PRAGMA table_info(decider_state)")}
        if "snoozed_until_ms" not in have:
            con.execute("ALTER TABLE decider_state ADD COLUMN "
                        "snoozed_until_ms INTEGER NOT NULL DEFAULT 0")
        if "meta" not in have:
            con.execute("ALTER TABLE decider_state ADD COLUMN meta TEXT")
        # What the phone has heard, and when — kept AFTER a condition closes,
        # which is the point: it is what stops a re-open from paging again.
        con.execute(
            "CREATE TABLE IF NOT EXISTS decider_pinged ("
            "  scope TEXT NOT NULL, kind TEXT NOT NULL, ms INTEGER NOT NULL,"
            "  PRIMARY KEY (scope, kind))")
        return con

    def pinged_ms(self, scope, kind) -> int:
        """When the phone last heard `kind` on `scope` (0 = never)."""
        if self.db is None:
            return int(self.mem_pinged.get((scope, kind), 0))
        con = self._con()
        try:
            r = con.execute("SELECT ms FROM decider_pinged WHERE scope=? AND kind=?",
                            (scope, kind)).fetchone()
            return int(r["ms"]) if r else 0
        finally:
            con.close()

    def mark_pinged(self, scope, kind, ms) -> None:
        if self.db is None:
            self.mem_pinged[(scope, kind)] = int(ms)
            return
        con = self._con()
        try:
            con.execute("INSERT INTO decider_pinged(scope, kind, ms) VALUES(?,?,?) "
                        "ON CONFLICT(scope, kind) DO UPDATE SET ms=excluded.ms",
                        (scope, kind, int(ms)))
            con.commit()
        finally:
            con.close()

    def get(self, scope):
        if self.db is None:
            return dict(self.mem[scope]) if scope in self.mem else None
        con = self._con()
        try:
            row = con.execute(
                "SELECT * FROM decider_state WHERE scope=?", (scope,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def put(self, scope, row: dict):
        if self.db is None:
            self.mem[scope] = dict(row)
            return
        con = self._con()
        try:
            con.execute(
                "INSERT INTO decider_state(scope,kind,first_ms,last_ms,count,"
                "reminded_ms,notified_ms,detail,snoozed_until_ms,meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET kind=excluded.kind,"
                " first_ms=excluded.first_ms, last_ms=excluded.last_ms,"
                " count=excluded.count, reminded_ms=excluded.reminded_ms,"
                " notified_ms=excluded.notified_ms, detail=excluded.detail,"
                " snoozed_until_ms=excluded.snoozed_until_ms, meta=excluded.meta",
                (scope, row["kind"], row["first_ms"], row["last_ms"],
                 row["count"], row["reminded_ms"], row["notified_ms"],
                 row.get("detail") or "", int(row.get("snoozed_until_ms") or 0),
                 _dumps(row.get("meta"))))
            con.commit()
        finally:
            con.close()

    def clear(self, scope):
        if self.db is None:
            self.mem.pop(scope, None)
            return
        con = self._con()
        try:
            con.execute("DELETE FROM decider_state WHERE scope=?", (scope,))
            con.commit()
        finally:
            con.close()

    def open_for_platform(self, platform=None):
        """Every open condition (for one platform, or all)."""
        if self.db is None:
            return [dict(r, scope=s) for s, r in self.mem.items()
                    if not platform or s.startswith(platform + ":")]
        con = self._con()
        try:
            if platform:
                rows = con.execute(
                    "SELECT * FROM decider_state WHERE scope LIKE ?",
                    (platform + ":%",)).fetchall()
            else:
                rows = con.execute("SELECT * FROM decider_state").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def _dumps(meta) -> str:
    import json
    try:
        return json.dumps(meta or {}, ensure_ascii=False)
    except Exception:
        return "{}"


def _loads(text) -> dict:
    import json
    if isinstance(text, dict):
        return dict(text)
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# -------------------------------------------------------------- the policy

def _wait_for(rule: Rule, count: int) -> int:
    """Fixed, or doubling per occurrence up to the ceiling."""
    if rule.max_wait_s:
        return int(min(rule.max_wait_s, rule.wait_s * (2 ** (max(1, count) - 1))))
    return int(rule.wait_s)


_PLAT = {"instagram": "IG", "facebook": "FB", "x": "X"}
_EMOJI = {"error": "🔴", "warn": "🟠", "info": "🔵"}


def _clip(text: str, n: int) -> str:
    """At most n characters, cut at a word, with an ellipsis when cut."""
    text = str(text or "")
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return (cut or text[:n]).rstrip(",;:(-") + "…"


def _plat(platform: str) -> str:
    return _PLAT.get((platform or "").lower(), (platform or "?").upper())


def _who_text(account: str, source: str = "") -> str:
    s = f"@{account}" if account else "collector"
    return s + (f" · {source}" if source else "")


def _links(cid: str) -> str:
    link = fix_url(cid)
    if link:
        return f"Fix → {link}\nSnooze 6h → {fix_url(cid, snooze_h=6)}"
    return "(set PUBLIC_BASE_URL in .env and this carries a link to the fix)"


def _ping_text(rule: Rule, ev, row: dict, now: int, cid: str) -> str:
    """The whole ping, in the fewest lines that still say what to do:

        🔴 IG @youssefnasser168 — CHECKPOINT — out of rotation, nothing knocks on it
        Do: Accounts & Sessions → @youssefnasser168 → Sign in → Open this account's browser → clear "confirm it's you"
        Now: Collection failed over to @sanaakhtar221.
        Fix → https://…/app/accounts?fix=instagram:youssefnasser168:checkpoint
        Snooze 6h → https://…&snooze=6

    Title, the first move, what the collector already did, the links. The
    paragraph this replaced (2026-09-06) said the same thing in 60 words and
    a phone showed the first 20."""
    fmt = dict(account=ev.account or "-", detail=_clip(row.get("detail") or "-", 110),
               open_for=_fmt_dur((now - row["first_ms"]) / 1000),
               count=row["count"])
    lines = [f"{_EMOJI.get(rule.level, '🟠')} {_plat(ev.platform)} "
             f"{_who_text(ev.account, ev.source)} — {rule.title.format(**fmt)}"]
    if rule.short:
        lines.append("Do: " + rule.short.format(**fmt))
    # "Now:" is ONE line: the first sentence of the note the collector
    # attached (a failover, a stop), never the advice paragraph the Fix
    # panel shows in full.
    note = (_loads(row.get("meta")).get("note") or "").strip()
    if note:
        first = note.split(". ")[0].rstrip(".")
        lines.append("Now: " + _clip(first, 160) + ".")
    lines.append(_links(cid))
    return "\n".join(lines)


def _still_open_text(rule: Rule, ev, row: dict, now: int, cid: str) -> str:
    return (f"🟠 {_plat(ev.platform)} {_who_text(ev.account, ev.source)} — still "
            f"'{ev.kind}' after {_fmt_dur((now - row['first_ms']) / 1000)} "
            f"({row['count']}×). Nothing new; this is the last ping about it.\n"
            f"{_links(cid)}")


def _recovered_text(platform, account, prev, now, how="") -> str:
    return (f"🟢 {_plat(platform)} {_who_text(account)} — recovered from "
            f"'{prev['kind']}'{how} after {_fmt_dur((now - prev['first_ms']) / 1000)}. "
            f"Collection continues.")


def _worth_telling(state, scope, prev: dict, now: int) -> bool:
    """Should the phone hear this condition END? Only if it heard it start —
    this instance was paged, or it was a re-open that was deliberately not
    paged twice (then only if it lasted RECOVERED_MIN_OPEN_S: a blip that
    closes in a minute was never news) — and not if a 'recovered' for this
    scope already went out within RECOVERED_DEDUPE_S."""
    meta = _loads(prev.get("meta"))
    if prev.get("notified_ms"):
        heard = True
    elif meta.get("not_paged_ms"):
        heard = (now - prev["first_ms"]) / 1000 >= RECOVERED_MIN_OPEN_S
    else:
        heard = False
    if not heard:
        return False
    last = state.pinged_ms(scope, "recovered")
    return not (last and now - last < RECOVERED_DEDUPE_S * 1000)


def decide(state: _State, ev: Event, now_ms=None, meta=None) -> Decision:
    """Pure policy: (open conditions, event) → decision. Persists the new
    condition state as a side effect on `state`, nothing else — no logging,
    no sending. The Decider wrapper below does those.

    `meta` is merged into the condition's stored meta (e.g. which sources
    belonged to a quarantined account, or which account collection failed
    over to); `meta["note"]` is appended to the operator text as {extra}.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rule = RULES.get(ev.kind)
    if rule is None:
        raise ValueError(f"unknown event kind {ev.kind!r}")
    prev = state.get(ev.scope)
    who = (f"[{ev.platform}{'@' + ev.account if ev.account else ''}"
           f"{' · ' + ev.source if ev.source else ''}]")

    # --- ok: close whatever was open for this scope -----------------------
    if ev.kind == "ok":
        d = Decision("collect", 0, "pass succeeded", kind="ok")
        if prev:
            d.say.append(
                f"{who} recovered from '{prev['kind']}' after "
                f"{_fmt_dur((now - prev['first_ms']) / 1000)} "
                f"({prev['count']} occurrence(s))")
            # The phone saw the start of this story; let it see the end —
            # once, and only for a story it saw (see _worth_telling).
            if _worth_telling(state, ev.scope, prev, now):
                d.notify = _recovered_text(
                    ev.platform, ev.account + (" · " + ev.source if ev.source else ""),
                    prev, now, " by itself")
                state.mark_pinged(ev.scope, "recovered", now)
            state.clear(ev.scope)
        return d

    # --- a different condition than the one open: close it, open this ----
    changed = prev is not None and prev["kind"] != ev.kind
    if prev is None or changed:
        # A change of kind is logged, never paged: the new condition pages
        # on its own terms if it is one that pages. (Until 2026-09-06 a paged
        # condition turning into another one sent "recovered (now X)" AND
        # then X's own ping — two messages for one change of state.)
        row = {"kind": ev.kind, "first_ms": now, "last_ms": now, "count": 1,
               "reminded_ms": now, "notified_ms": 0, "detail": ev.detail,
               "snoozed_until_ms": 0, "meta": {}}
    else:
        row = dict(prev)
        row["last_ms"] = now
        row["count"] = int(prev["count"]) + 1
        row["detail"] = ev.detail or prev.get("detail") or ""
        row["meta"] = _loads(prev.get("meta"))
        d_note = ""
    if meta:
        row["meta"] = {**_loads(row.get("meta")), **meta}
    snoozed = int(row.get("snoozed_until_ms") or 0) > now

    open_for_s = (now - row["first_ms"]) / 1000
    count = row["count"]

    wait = _wait_for(rule, count)

    cid = cond_id(ev.platform, ev.account, ev.kind, ev.source)
    d = Decision(rule.action, int(wait),
                 reason=f"{ev.kind}: {ev.detail}" if ev.detail else ev.kind,
                 kind=ev.kind, count=count, open_since_ms=row["first_ms"],
                 cond_id=cid)

    detail = f" — {ev.detail}" if ev.detail else ""
    if snoozed:
        pass                    # the operator asked for quiet; keep counting
    elif prev is None or changed:
        pre = (f"(was '{prev['kind']}' for "
               f"{_fmt_dur((now - prev['first_ms']) / 1000)}) " if changed else "")
        if not rule.title:
            tell = ""
        elif rule.escalate_after_n:
            tell = f"; operator will be told after {rule.escalate_after_n} in a row"
        elif rule.escalate_after_s == 0:
            tell = "; operator will be told now"
        elif rule.escalate_after_s > 0:
            tell = f"; operator will be told if still open in {_fmt_dur(rule.escalate_after_s)}"
        else:
            tell = ""
        nxt = (f" — next try in {_fmt_dur(wait)}" if wait and rule.loop_wait
               else f" — skipped for {_fmt_dur(wait)}, the other sources continue"
               if wait else " — skipped, the other sources continue")
        d.say.append(
            f"{who} decision: {rule.action.upper()} {pre}on '{ev.kind}'{detail}"
            f"{nxt}{tell}")
    elif now - row["reminded_ms"] >= rule.remind_every_s * 1000:
        row["reminded_ms"] = now
        d.say.append(
            f"{who} still '{ev.kind}' — open for {_fmt_dur(open_for_s)}, "
            f"{count} occurrence(s), {rule.action} continues, next try in "
            f"{_fmt_dur(wait)}")

    # Page the operator — once per open condition, never while snoozed, and
    # never twice within REPING_COOLDOWN_S for the same condition on the same
    # scope, however many times it closed and re-opened in between.
    mt = _loads(row.get("meta"))
    told = bool(row["notified_ms"]) or bool(mt.get("not_paged_ms"))
    if rule.title and not told and not snoozed:
        due = False
        if rule.escalate_after_n:
            due = count >= rule.escalate_after_n
        elif rule.escalate_after_s >= 0:
            due = open_for_s >= rule.escalate_after_s
        if due:
            last = state.pinged_ms(ev.scope, ev.kind)
            if last and now - last < REPING_COOLDOWN_S * 1000:
                mt["not_paged_ms"] = now
                d.say.append(
                    f"{who} operator NOT paged again — '{ev.kind}' was paged "
                    f"{_fmt_dur((now - last) / 1000)} ago; the Fix panel has it")
            else:
                row["notified_ms"] = now
                d.notify = _ping_text(rule, ev, row, now, cid)
                state.mark_pinged(ev.scope, ev.kind, now)
    elif (rule.title and rule.needs_human and row["notified_ms"] and not snoozed
          and not mt.get("still_open_ms")
          and open_for_s >= STILL_OPEN_AFTER_S):
        # ONE "still open" ping, then nothing more until it closes.
        mt["still_open_ms"] = now
        d.notify = _still_open_text(rule, ev, row, now, cid)
    row["meta"] = mt

    state.put(ev.scope, row)
    return d


# ------------------------------------------------ the panel's view of it

def open_conditions(platform=None, *, db=DEFAULT_DB, now_ms=None,
                    state=None) -> list:
    """What is open right now, shaped for the dashboard's Fix panel."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out = []
    for r in (state or _State(db)).open_for_platform(platform):
        plat, who = (r["scope"].split(":", 1) + [""])[:2]
        acct, src = _split_who(who)
        rule = RULES.get(r["kind"])
        if rule is None:
            continue
        cid = cond_id(plat, acct, r["kind"], src)
        meta = _loads(r.get("meta"))
        fmt = dict(account=acct or "-", detail=r.get("detail") or "-",
                   open_for=_fmt_dur((now - r["first_ms"]) / 1000),
                   count=r["count"], extra="")
        out.append({
            "id": cid, "platform": plat, "account": acct, "source": src,
            "kind": r["kind"],
            "action": rule.action, "level": rule.level,
            "needs_human": rule.needs_human,
            "since_ms": r["first_ms"], "last_ms": r["last_ms"],
            "count": r["count"], "detail": r.get("detail") or "",
            "notified_ms": r.get("notified_ms") or 0,
            "snoozed_until_ms": r.get("snoozed_until_ms") or 0,
            "steps": [step.format(**fmt) for step in rule.fix],
            "actions": list(rule.actions),
            "meta": meta,
            "fix_url": fix_url(cid),
        })
    out.sort(key=lambda c: (not c["needs_human"], c["since_ms"]))
    return out


def snooze(cid: str, hours: float = 6, *, db=DEFAULT_DB, now_ms=None) -> dict:
    """Silence one open condition: no reminders, no ping, for `hours`.
    The collector keeps deciding (idle/backoff) — only the talking stops."""
    plat, who, kind = parse_cond_id(cid)
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    st = _State(db)
    scope = f"{plat}:{who}"
    row = st.get(scope)
    if not row or row["kind"] != kind:
        return {"ok": False, "error": "that condition is no longer open"}
    row["snoozed_until_ms"] = now + int(float(hours) * H * 1000)
    row["meta"] = _loads(row.get("meta"))
    st.put(scope, row)
    return {"ok": True, "snoozed_until_ms": row["snoozed_until_ms"]}


def resolve(cid: str, *, db=DEFAULT_DB, who="operator", log=None,
            notify=None, now_ms=None, quiet=False) -> dict:
    """Close one open condition by hand. Logs it, and if the phone was pinged
    about it, pings once more so the story ends there too — unless `quiet`:
    a sign-in closes conditions as a STEP of the fix, not as its outcome (the
    outcome is the next clean read, which pages 'recovered' by itself), and
    paging on the step is how one checkpoint became five messages."""
    plat, whom, kind = parse_cond_id(cid)
    acct, src = _split_who(whom)
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    st = _State(db)
    scope = f"{plat}:{whom}"
    row = st.get(scope)
    if not row or row["kind"] != kind:
        return {"ok": False, "error": "that condition is no longer open"}
    st.clear(scope)
    line = (f"[{plat}{'@' + acct if acct else ''}{' · ' + src if src else ''}] "
            f"'{kind}' resolved by {who} "
            f"after {_fmt_dur((now - row['first_ms']) / 1000)} "
            f"({row['count']} occurrence(s))")
    if log:
        try:
            log(line)
        except Exception:
            pass
    if not quiet and _worth_telling(st, scope, row, now):
        text = _recovered_text(plat, acct + (" · " + src if src else ""), row, now,
                               f" by {who}")
        try:
            (notify or _default_notify)(text)
            st.mark_pinged(scope, "recovered", now)
        except Exception:
            pass
    return {"ok": True, "message": line}


# ------------------------------------------------------------- the wrapper

def admin_chat() -> str:
    """The ADMIN — the one person every decision, permission request and
    update goes to. ADMIN_TELEGRAM_CHAT_ID in .env; falls back to the
    delivery chat (TELEGRAM_CHAT_ID) so an older .env still pages someone.
    Set explicitly on purpose: the delivery chat is often a group that sees
    posts, and a "sign this account in" request does not belong there."""
    return (os.getenv("ADMIN_TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHAT_ID", "").strip())


def admin_name() -> str:
    return os.getenv("ADMIN_NAME", "").strip() or "Admin"


def admin_token() -> str:
    """The ADMIN BOT — its own bot, for the one person the collector pages
    (ADMIN_TELEGRAM_BOT_TOKEN, "Vedic Scraper Admin"). Falls back to the
    delivery bot (TELEGRAM_BOT_TOKEN) so an older .env still pages someone;
    set explicitly on purpose: the delivery bot posts collected content to
    a group, and "sign this account in" does not belong in that voice."""
    return (os.getenv("ADMIN_TELEGRAM_BOT_TOKEN", "").strip()
            or os.getenv("TELEGRAM_BOT_TOKEN", "").strip())


def notify_ready() -> bool:
    return bool(admin_token() and admin_chat())


def _tg(token: str, method: str, payload: dict, timeout=10.0):
    """One Telegram Bot API call → (ok, json_or_error)."""
    import httpx
    rep = httpx.post(f"https://api.telegram.org/bot{token}/{method}",
                     json=payload, timeout=timeout)
    try:
        body = rep.json()
    except Exception:
        body = {"description": rep.text}
    if rep.status_code == 200 and body.get("ok", True):
        return True, body
    return False, f"HTTP {rep.status_code}: {str(body.get('description') or body)[:200]}"


def send_admin(text: str, *, token: str = "", chat: str = ""):
    """Send one message to the admin through the admin bot → (ok, error)."""
    token = token or admin_token()
    chat = chat or admin_chat()
    if not token or not chat:
        return False, "ADMIN_TELEGRAM_BOT_TOKEN / ADMIN_TELEGRAM_CHAT_ID not set"
    try:
        ok, body = _tg(token, "sendMessage",
                       {"chat_id": chat, "text": text,
                        "disable_web_page_preview": True})
        return (True, "") if ok else (False, body)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def discover_chat(token: str = ""):
    """The private chat that last wrote to the admin bot → (chat_id, name),
    or ("", why). Telegram will not let a bot start a conversation, so the
    admin presses Start (or sends any message) once; this reads it back so
    nobody has to look up their own chat id."""
    token = token or admin_token()
    if not token:
        return "", "no admin bot token"
    try:
        ok, body = _tg(token, "getUpdates", {"limit": 100, "timeout": 0})
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"
    if not ok:
        return "", str(body)
    for upd in reversed(body.get("result") or []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id"):
            name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
                or chat.get("username") or ""
            return str(chat["id"]), name
    return "", ("nobody has messaged the bot yet — open it in Telegram, press "
                "Start, then try again")


def _default_notify(text: str):
    """Telegram, to the admin, through the admin bot — the message as
    composed, no prefix (the bot's own name is the sender line)."""
    return send_admin(text)


class Decider:
    """What a collector holds: `on(event)` → Decision, already logged and
    (when due) sent. `wait_s()` is the longest wait any decision in the
    current pass asked for, so the loop sleeps as the policy says.

        dec = decider.Decider("instagram", log=log)          # persistent
        dec = decider.Decider("instagram", log=log, db=None) # one-shot

    `notify` is injectable so the tests record instead of sending; `now` is
    a callable returning ms so the tests can move the clock.
    """

    def __init__(self, platform="instagram", *, log=print, db=DEFAULT_DB,
                 notify=None, now=None, quiet=False):
        self.platform = platform
        self.log = log
        self.state = _State(db)
        self.notify = notify or _default_notify
        self.now = now
        # quiet: decide and log, never page. The dashboard's Fetch-now and
        # the CLI's one-shot run use it — the operator is looking at the
        # screen that shows the reason, and a one-shot decider has no memory
        # of having paged, so until 2026-09-06 every click paged again. The
        # service loop's persistent decider pages once if the condition
        # persists.
        self.quiet = bool(quiet)
        self._pass_wait = 0
        self._platform_wait = 0
        self._said_nowhere = False
        self._said_quiet = False

    def begin_pass(self):
        self._pass_wait = 0
        self._platform_wait = 0

    def wait_s(self, default=0) -> int:
        """The longest wait a decision in this pass asked for (0 = none)."""
        return self._pass_wait or int(default)

    def platform_wait_s(self, default=0) -> int:
        """The longest wait a PLATFORM-level decision (paused, no sources, a
        pass that blew up) asked for this pass. The parallel loop sleeps on
        this one; an account's own backoff is served by account_wait() and
        must not idle its neighbours."""
        return self._platform_wait or int(default)

    def on(self, kind, account="", detail="", exc=None, meta=None,
           source="") -> Decision:
        if exc is not None and not kind:
            kind = classify_exception(exc)
            detail = detail or f"{type(exc).__name__}: {exc}"
        ev = Event(kind, self.platform, account, str(detail)[:400], source)
        with _LOCK:
            d = decide(self.state, ev, self.now() if self.now else None, meta)
        for line in d.say:
            self._log(line)
        if d.notify and self.quiet:
            if not self._said_quiet:
                self._said_quiet = True
                self._log(f"[{self.platform}] operator not paged from a one-shot "
                          f"pass — the service loop pages once if this persists")
        elif d.notify:
            ok, err = self.notify(d.notify)
            if ok:
                self._log(f"[{self.platform}] operator told (Telegram): "
                          f"{'recovered' if ev.kind == 'ok' else ev.kind}")
            elif not self._said_nowhere:
                self._said_nowhere = True
                self._log(f"[{self.platform}] operator NOT told — {err}")
        if RULES[ev.kind].loop_wait:
            self._pass_wait = max(self._pass_wait, d.wait_s)
            if not ev.account:
                self._platform_wait = max(self._platform_wait, d.wait_s)
        return d

    def holdoff(self, account="", source="") -> int:
        """Seconds this scope should still be left alone, 0 if it may be
        tried now. For a per-source condition (unresolved id) the collector
        asks this BEFORE spending a request, so a handle that was refused an
        hour ago is not asked about again every pass — that repetition is
        what keeps a 429 alive."""
        row = self.state.get(f"{self.platform}:{_who(account, source)}")
        if not row:
            return 0
        rule = RULES.get(row["kind"])
        if rule is None or rule.loop_wait:
            return 0
        now = self.now() if self.now else int(time.time() * 1000)
        until = int(row["last_ms"]) + _wait_for(rule, int(row["count"])) * 1000
        return max(0, int((until - now) / 1000))

    def account_wait(self, account: str) -> int:
        """Seconds @account should still REST because of an open condition of
        its own (rate limit, proxy, session, checkpoint, budget) — the
        per-account form of wait_s(), for a loop that runs accounts in
        parallel: one account backing off must not idle the others, and an
        account that was told to wait 4h must not be woken by a neighbour's
        pass. 0 when it may run now. Platform-wide conditions (paused, no
        sources) are not counted here — the loop asks wait_s() for those."""
        best = 0
        for row in self.state.open_for_platform(self.platform):
            scope = row.get("scope") or ""
            who = scope.split(":", 1)[1] if ":" in scope else ""
            if _split_who(who)[0] != account or _split_who(who)[1]:
                continue
            rule = RULES.get(row["kind"])
            if rule is None or not rule.loop_wait or rule.wait_s <= 0:
                continue
            now = self.now() if self.now else int(time.time() * 1000)
            until = int(row["last_ms"]) + _wait_for(rule, int(row["count"])) * 1000
            best = max(best, int((until - now) / 1000))
        return max(0, best)

    def ok(self, account="", source="") -> Decision:
        return self.on("ok", account, source=source)

    def fold(self, account: str, kind: str, into: str, keep=lambda meta: True) -> list:
        """Close every open per-source `kind` condition on `account` whose meta
        satisfies `keep`, quietly (one log line), because a broader condition
        `into` now covers them. This is what turns eight "handle needs its
        id" cards that all say 429 into one "lookups throttled" card."""
        gone = []
        for c in self.open_conditions():
            if c["account"] != account or c["kind"] != kind or not c["source"]:
                continue
            if not keep(c.get("meta") or {}):
                continue
            self.state.clear(f"{self.platform}:{_who(account, c['source'])}")
            gone.append(c["source"])
        if gone:
            self._log(f"[{self.platform}@{account}] {len(gone)} '{kind}' "
                      f"condition(s) folded into '{into}': {', '.join(sorted(gone))}")
        return gone

    def open_conditions(self) -> list:
        return open_conditions(self.platform, state=self.state,
                               now_ms=self.now() if self.now else None)

    def _log(self, line):
        try:
            self.log(line)
        except Exception:
            pass


# ------------------------------------------------------------------ the CLI

def _cli(argv) -> int:
    """
    python3 decider.py test    send one test ping through the admin bot
    python3 decider.py chat    print the chat id of whoever last wrote to
                               the admin bot (press Start on it first)

    Reads .env from the working directory like the services do.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    cmd = (argv[1] if len(argv) > 1 else "").strip()
    if cmd == "chat":
        cid, name = discover_chat()
        if not cid:
            print(f"no chat found: {name}")
            return 1
        print(f"ADMIN_TELEGRAM_CHAT_ID={cid}    # {name}")
        return 0
    if cmd == "test":
        chat = admin_chat()
        if not chat:
            chat, name = discover_chat()
            if not chat:
                print(f"no admin chat: {name}")
                return 1
            print(f"(no ADMIN_TELEGRAM_CHAT_ID set — using {chat} / {name} from the bot's updates; "
                  f"put it in .env to make it permanent)")
        ok, err = send_admin(
            f"🟢 Vedic Scraper Admin is connected. Pings arrive here, one per "
            f"condition, with the first move and a link to the fix.", chat=chat)
        print("sent" if ok else f"not sent: {err}")
        return 0 if ok else 1
    if cmd not in ("", "-h", "--help"):
        print(f"unknown command {cmd!r}")
    print(_cli.__doc__)
    return 0 if cmd in ("", "-h", "--help") else 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv))
