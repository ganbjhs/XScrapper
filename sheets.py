"""
sheets.py — delivering collected posts into a Google Sheet.

A third delivery target beside webhooks and Telegram, and deliberately the
thinnest of the three: everything hard about delivery — the cursor, the
back-off, "never block collection", per-target filters — already lives in
webhook.py and is shared unchanged. All that is new here is a transport and a
row shape.

    collector stores a tweet  ->  sender notices  ->  values.append -> your sheet

FOUR COLUMNS, ALWAYS: date | link | text | media
Written once as a header row when the tab is empty, then appended to.

ORDER. Rows ARRIVE in collection order, which is not the order anyone wants to
read: a backwards sweep collects 2024 while live polling adds today, so a pure
append interleaves them. Via the Apps Script route the sheet is re-sorted on
arrival so the newest post sits at the top and the oldest at the bottom, and
duplicate links are dropped so an overlapping "send past posts" is harmless.
Both are switches at the top of the script. The service-account route still
appends in collection order -- see RULEBOOK 4; the two routes do not currently
produce the same sheet.

WHY A HAND-ROLLED SERVICE-ACCOUNT JWT AND NOT google-auth
---------------------------------------------------------
google-auth's refresh is synchronous and drags in `requests` or `urllib3` as a
transport; this project's delivery loop is async httpx from top to bottom and
pins its dependencies deliberately (see requirements.txt). Signing the
assertion ourselves is ~40 lines against `cryptography`, which is ALREADY a
dependency, and keeps every network call on the one client the sender owns.
The signing itself is not a place with room for cleverness — RS256 over
"<header>.<claims>", which is the whole of RFC 7515 that we need.

SETUP, ONCE
-----------
  1. Google Cloud console -> a project -> enable the Google Sheets API.
  2. Create a SERVICE ACCOUNT, add a JSON key, download it.
  3. Put it on the server and name its path in .env:
         GOOGLE_SHEETS_CREDENTIALS=/etc/xscraper/google-sheets.json
     (the JSON itself also works if you would rather not have a file).
  4. SHARE THE SHEET with the service account's client_email, as Editor.
     This is the step everyone forgets, and its symptom is a 403 with
     "The caller does not have permission" — see check_access().
"""

import base64
import json
import os
import time

# The narrowest scope that can append. drive.file would additionally let us
# create spreadsheets; we never do, because a sheet the operator made and
# shared is a sheet the operator can find again.
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://sheets.googleapis.com/v4/spreadsheets"

CREDS_ENV = "GOOGLE_SHEETS_CREDENTIALS"

# The columns, in order. Changing this list changes the header written into a
# fresh tab AND the row builder — they are the same list on purpose, because
# the one bug this feature can have is a header that stops describing the
# columns underneath it.
HEADER = ["date", "link", "text", "media"]
COLS = "A:D"                       # must span exactly len(HEADER) columns

# Dates are written for a human to read, so they are written in the operator's
# timezone, not UTC. Asia/Kolkata because that is where this runs; override in
# .env if it moves.
TZ_ENV = "SHEET_TZ"
TZ_DEFAULT = "Asia/Kolkata"

# Access tokens live an hour; refresh a minute early so a delivery never
# arrives at the API with a token that expired in flight.
TOKEN_SKEW_S = 60

_tokens: dict = {}                 # client_email -> (token, expires_at)
_header_done: set = set()          # (spreadsheet_id, tab) already checked


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def creds_env() -> str:
    return os.getenv(CREDS_ENV, "").strip()


def load_creds() -> dict | None:
    """
    The service-account key, from a path or from the JSON itself.

    Returns None rather than raising: a missing or broken key is a
    configuration problem the caller reports and skips past, never something
    that takes the delivery loop down with every other target on it.
    """
    raw = creds_env()
    if not raw:
        return None
    try:
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
        else:
            with open(os.path.expanduser(raw), "r", encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not data.get("client_email") or not data.get("private_key"):
        return None
    return data


def _b64(raw: bytes) -> str:
    """base64url, unpadded — what JWS requires and what base64 does not do."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _assertion(creds: dict, now: int) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"},
                           separators=(",", ":")).encode())
    body = _b64(json.dumps({
        "iss": creds["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()

    key = serialization.load_pem_private_key(
        creds["private_key"].encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


async def access_token(client, creds: dict) -> tuple[str, str]:
    """
    (token, error). Cached per service account until a minute before expiry.

    Never raises — every caller here is inside the delivery loop.
    """
    now = int(time.time())
    hit = _tokens.get(creds["client_email"])
    if hit and hit[1] - TOKEN_SKEW_S > now:
        return hit[0], ""
    try:
        assertion = _assertion(creds, now)
    except Exception as e:
        return "", f"service-account key is unusable: {type(e).__name__}: {e}"
    try:
        rep = await client.post(TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }, timeout=20.0)
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"
    if rep.status_code != 200:
        return "", f"Google refused the service account: {_why(rep)}"
    try:
        data = rep.json()
        token = data["access_token"]
        expires = now + int(data.get("expires_in") or 3600)
    except (ValueError, KeyError) as e:
        return "", f"unreadable token response: {type(e).__name__}: {e}"
    _tokens[creds["client_email"]] = (token, expires)
    return token, ""


def _why(rep) -> str:
    """Google's own explanation beats 'HTTP 403' every single time."""
    try:
        j = rep.json()
        msg = (j.get("error", {}).get("message")
               if isinstance(j.get("error"), dict) else j.get("error_description")
               or j.get("error"))
    except Exception:
        msg = None
    return f"HTTP {rep.status_code}: {str(msg or rep.text or '')[:200]}"


# --------------------------------------------------------------------------
# addressing
# --------------------------------------------------------------------------

def sheet_id(text: str) -> str:
    """
    The id, whether you were given an id or the URL you copied from the
    address bar. Operators paste the URL, so accepting only the id would
    fail a step earlier than the failure they can see.
    """
    s = (text or "").strip()
    if "/spreadsheets/d/" in s:
        s = s.split("/spreadsheets/d/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return s.strip()


def a1(tab: str, cols: str = COLS) -> str:
    """
    An A1 range for a tab whose name we do not control.

    Tab names may contain spaces, and may contain a single quote — which is
    also the quoting character, so it is doubled. Getting this wrong sends the
    rows to the wrong tab or fails the request, and both look like a bug in
    delivery rather than in a string.
    """
    name = (tab or "").strip() or "Sheet1"
    return f"'{name.replace(chr(39), chr(39) * 2)}'!{cols}"


# --------------------------------------------------------------------------
# the row
# --------------------------------------------------------------------------

def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv(TZ_ENV, "").strip() or TZ_DEFAULT)
    except Exception:
        from datetime import timezone
        return timezone.utc


def fmt_date(row: dict) -> str:
    """
    When the post was PUBLISHED, in local time, as 'YYYY-MM-DD HH:MM:SS'.

    created_ms, not collected_ms: a sheet sorted by its first column should
    read as the author's timeline, not as a log of when our poller happened to
    notice. The format is the one Sheets parses into a real datetime, so the
    column sorts and filters as a date instead of as text.
    """
    from datetime import datetime, timezone
    ms = row.get("created_ms") or 0
    if not ms:
        iso = (row.get("created_at") or "").strip()
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return iso
    else:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz()).strftime("%Y-%m-%d %H:%M:%S")


def fmt_media(row: dict) -> str:
    """
    Every image and video URL on the post, one per line.

    media_json is preferred over media_urls because it is the structured view
    the dashboard renders from — it distinguishes a video from its thumbnail,
    where media_urls is a flat list. A post with no media gets an empty cell,
    not the string "[]".
    """
    try:
        media = json.loads(row.get("media_json") or "[]")
    except (TypeError, ValueError):
        media = []
    urls = [m.get("url") for m in media
            if isinstance(m, dict) and m.get("url")]
    if not urls:
        try:
            urls = [u for u in json.loads(row.get("media_urls") or "[]") if u]
        except (TypeError, ValueError):
            urls = []
    return "\n".join(urls)


def _safe(value: str) -> str:
    """
    Defuse a cell that Sheets would otherwise read as a formula.

    Rows are written with valueInputOption=USER_ENTERED so the date column
    lands as a real datetime and the link column is clickable — which also
    means a post beginning "=" or "+" would be evaluated. Tweets beginning
    "-" are ordinary. A leading apostrophe is Sheets' own "this is text"
    marker: it forces the literal and is not displayed.
    """
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def sheet_row(row: dict) -> list:
    """One post as [date, link, text, media] — the order in HEADER."""
    link = row.get("url") or f"https://x.com/i/status/{row.get('tweet_id')}"
    return [fmt_date(row), link, _safe((row.get("text") or "").strip()),
            fmt_media(row)]


def sheet_rows(rows: list) -> list:
    return [sheet_row(r) for r in rows]


# --------------------------------------------------------------------------
# the API calls
# --------------------------------------------------------------------------

async def _get_values(client, token: str, sid: str, rng: str):
    return await client.get(
        f"{API}/{sid}/values/{rng}",
        headers={"Authorization": f"Bearer {token}"}, timeout=30.0)


async def ensure_header(client, token: str, sid: str, tab: str) -> tuple[bool, str]:
    """
    Write the header row, but only into a tab that is empty.

    Checked once per (sheet, tab) per process, and skipped entirely once the
    first row exists — so an operator who renamed a column, or who is
    appending to a sheet that already had data, keeps what they have. We are
    a guest in someone else's spreadsheet.
    """
    key = (sid, tab)
    if key in _header_done:
        return True, ""
    rep = await _get_values(client, token, sid, a1(tab, "A1:D1"))
    if rep.status_code != 200:
        return False, _access_hint(rep, sid, tab)
    try:
        existing = rep.json().get("values") or []
    except ValueError:
        existing = []
    if existing and any(str(c).strip() for c in (existing[0] or [])):
        _header_done.add(key)
        return True, ""
    try:
        put = await client.put(
            f"{API}/{sid}/values/{a1(tab, 'A1:D1')}",
            params={"valueInputOption": "RAW"},
            json={"values": [HEADER]},
            headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if put.status_code != 200:
        return False, f"could not write the header row: {_why(put)}"
    _header_done.add(key)
    return True, ""


def _access_hint(rep, sid: str, tab: str) -> str:
    """
    Turn Google's two most common refusals into the fix.

    A 403 here is almost never a broken key — it is a sheet nobody shared, and
    a 400 naming the range is almost always a tab that does not exist under
    that name. Saying so is the difference between a two-minute fix and an
    afternoon.
    """
    detail = _why(rep)
    if rep.status_code == 403:
        return (f"{detail} — share the sheet with the service account's "
                f"client_email (Editor) and try again")
    if rep.status_code == 404:
        return f"{detail} — no spreadsheet with id {sid}"
    if rep.status_code == 400 and "range" in detail.lower():
        return f'{detail} — is there a tab called "{tab}"?'
    return detail


async def append(client, token: str, sid: str, tab: str,
                 values: list) -> tuple[bool, str]:
    """
    Append rows to the bottom of the tab. Never raises.

    insertDataOption=INSERT_ROWS rather than OVERWRITE: overwrite would search
    for the first empty row and can walk INTO a table someone left a gap in.
    Insert always adds new rows at the end of the data, which is the only
    behaviour that is safe to run unattended forever.
    """
    if not values:
        return True, ""
    try:
        rep = await client.post(
            f"{API}/{sid}/values/{a1(tab)}:append",
            params={"valueInputOption": "USER_ENTERED",
                    "insertDataOption": "INSERT_ROWS"},
            json={"values": values},
            headers={"Authorization": f"Bearer {token}"}, timeout=45.0)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if 200 <= rep.status_code < 300:
        return True, ""
    return False, _access_hint(rep, sid, tab)


async def via_api(client, sid: str, tab: str, rows: list) -> tuple[bool, str]:
    """
    One batch, end to end: token -> header -> append. (ok, error).

    The whole batch is one append call, so a batch either lands completely or
    not at all and the cursor — which advances only on ok — cannot leave half
    of it behind.
    """
    creds = load_creds()
    if not creds:
        return False, f"{CREDS_ENV} is not set, or does not point at a usable " \
                      f"service-account JSON key"
    token, err = await access_token(client, creds)
    if err:
        return False, err
    ok, err = await ensure_header(client, token, sid, tab)
    if not ok:
        return False, err
    return await append(client, token, sid, tab, sheet_rows(rows))


async def check_api_access(client, sid: str, tab: str) -> tuple[bool, str]:
    """
    Prove the whole path works WITHOUT writing a post into the sheet.

    Reads the tab (which needs the key, the share and the tab name to all be
    right) and writes the header if it is empty. What is left after a
    successful check is a sheet ready to receive, not a sheet with a fake row
    in it someone has to delete.
    """
    creds = load_creds()
    if not creds:
        return False, f"{CREDS_ENV} is not set in .env on the server"
    token, err = await access_token(client, creds)
    if err:
        return False, err
    return await ensure_header(client, token, sid, tab)


def service_account_email() -> str:
    """Shown in the UI, because it is the address the sheet must be shared with."""
    creds = load_creds()
    return (creds or {}).get("client_email", "")


# --------------------------------------------------------------------------
# Apps Script mode — the sheet runs its own receiver
# --------------------------------------------------------------------------
#
# The whole point: the script executes AS THE SHEET'S OWNER, so the awkward
# half of the REST path — a cloud project, a JSON key, a sharing step, and a
# credential of ours that can reach every sheet that account was ever given —
# simply does not exist. What we hold is a URL and a token for ONE sheet.
#
# THE /exec URL IS NOT THE SECRET. Google's deployment URLs are long but they
# are addresses, and this one has to be reachable by "Anyone" for us to POST
# to it at all. The token in the body is what makes a request legitimate, and
# it lives in .env by name — the same rule webhooks follow, for the same
# reason: a credential in the database is a credential in every backup of it.
#
# The trade against the REST path, stated honestly: this is a shared secret
# compared for equality, not a signature over the body, so it does not survive
# someone reading the request. Over TLS to a Google-hosted endpoint, for the
# capability "append rows to one spreadsheet", that is the same bargain the
# Telegram bot token already makes and it is a fair one.

SCRIPT_URL_PREFIX = "https://script.google.com/"

# The paste-into-the-sheet half of this feature. Kept HERE, beside the code
# that talks to it, because the two are one protocol: change the body shape in
# via_script() and this must change in the same commit or every sheet silently
# stops accepting rows.
SCRIPT_SOURCE = r"""/**
 * X Collector -> this spreadsheet.
 *
 * Deploy:  Deploy > New deployment > Web app
 *            Execute as:      Me
 *            Who has access:  Anyone
 *          Copy the /exec URL it gives you back into the dashboard.
 *
 * "Anyone" is safe here BECAUSE of the token below: the URL accepts requests
 * from anywhere, and then ignores every one that does not carry it.
 */

var TOKEN = '%TOKEN%';
var HEADER = ['date', 'link', 'text', 'media'];

// Keep the newest post at the top and the oldest at the bottom, by POST date.
//
// Rows arrive in COLLECTION order, which is not the order anyone wants to read.
// A backwards sweep collects history newest-first while live polling keeps
// adding today's posts, so a pure append interleaves them: a 2024 post, then a
// 2026 one, then more 2024. Sorting on arrival is what makes the sheet read as
// a timeline instead of as a delivery log.
//
// The cost, stated plainly: rows MOVE. If you are scrolled into the middle of
// the sheet when a batch lands, what is under your cursor shifts. Set this to
// false to get the old append-only behaviour back, where a row that lands is
// never touched again.
var SORT_NEWEST_FIRST = true;

// Never write the same post twice.
//
// "Send past posts" does not move the delivery cursor, so a window that
// overlaps what was already delivered would otherwise duplicate every post in
// it -- and the natural way to fill a gap is to ask for a generous window.
// Matching on the link column makes the whole endpoint idempotent: send the
// same range as often as you like, only genuinely new posts land.
var SKIP_DUPLICATES = true;

function doPost(e) {
  // One writer at a time. A live batch and a "send past posts" run can
  // overlap, and two appends computing the same last row would overwrite
  // each other.
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
  } catch (err) {
    return reply({ error: 'busy — another batch is still writing' });
  }
  try {
    var body = JSON.parse(e.postData.contents);
    if (!TOKEN || body.token !== TOKEN) {
      return reply({ error: 'bad token' });
    }
    var name = String(body.tab || 'Sheet1');
    var book = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = book.getSheetByName(name) || book.insertSheet(name);

    // The header goes in only when the tab is empty, so a sheet someone has
    // already shaped keeps the headings they gave it.
    if (sheet.getLastRow() === 0) {
      sheet.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
    }

    var rows = body.rows || [];
    var skipped = 0;

    if (rows.length && SKIP_DUPLICATES && sheet.getLastRow() > 1) {
      // Column B is the post link -- unique per post, and already text.
      var seen = {};
      var links = sheet.getRange(2, 2, sheet.getLastRow() - 1, 1).getValues();
      for (var i = 0; i < links.length; i++) {
        var v = String(links[i][0] || '');
        if (v) { seen[v] = true; }
      }
      var fresh = [];
      for (var j = 0; j < rows.length; j++) {
        var link = String(rows[j][1] || '');
        if (link && seen[link]) { skipped++; continue; }
        if (link) { seen[link] = true; }   // guard against dupes inside one batch
        fresh.push(rows[j]);
      }
      rows = fresh;
    }

    if (rows.length) {
      sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, HEADER.length)
           .setValues(rows);
    }

    // Sort AFTER appending, over everything below the header. Done even when
    // this batch added nothing new, so a sheet that predates this script gets
    // put in order by the first delivery that reaches it.
    if (SORT_NEWEST_FIRST && sheet.getLastRow() > 2) {
      sheet.getRange(2, 1, sheet.getLastRow() - 1, HEADER.length)
           .sort({ column: 1, ascending: false });
    }

    return reply({ ok: true, appended: rows.length, skipped: skipped, tab: name });
  } catch (err) {
    return reply({ error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  // Visiting the URL in a browser should say something, not 404. It
  // deliberately does not confirm the token.
  return reply({ ok: true, note: 'X Collector sheet endpoint. POST only.' });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
                       .setMimeType(ContentService.MimeType.JSON);
}
"""


def script_source(token: str) -> str:
    """The script to paste, with the operator's own token already in it."""
    return SCRIPT_SOURCE.replace("%TOKEN%", (token or "").replace("'", ""))


def new_token() -> str:
    """
    A fresh token for a new deployment.

    Generated for the operator rather than invented by them, because a token
    someone types is a token someone reuses, and this one is the only thing
    standing in front of an endpoint Google requires to be world-reachable.
    """
    import secrets
    return secrets.token_urlsafe(32)


def _script_hint(rep, body_text: str) -> str:
    """
    Turn a non-answer from Apps Script into the setting that is wrong.

    Every failure here is a deployment setting, and every one of them looks
    the same from Python — an HTML page where JSON was expected. Naming the
    likely cause is the difference between a two-minute fix and giving up on
    the feature.
    """
    text = (body_text or "").strip()
    if text[:1] == "<" or "<!DOCTYPE" in text[:200].upper():
        return ('Google served a sign-in page instead of the script. In Apps '
                'Script, redeploy with "Who has access: Anyone" — with '
                '"Only myself" the URL asks a human to log in, which a server '
                'cannot do.')
    if rep is not None and rep.status_code == 404:
        return ('HTTP 404 — that /exec URL does not exist. Copy it again from '
                'Deploy > Manage deployments (a NEW deployment gets a new URL).')
    if rep is not None and not (200 <= rep.status_code < 300):
        return f"HTTP {rep.status_code}: {text[:200]}"
    return f"the script replied with something that is not JSON: {text[:200]}"


async def via_script(client, exec_url: str, token: str, tab: str,
                     rows: list, info: dict | None = None) -> tuple[bool, str]:
    """
    Hand a batch to the sheet's own script. (ok, error). Never raises.

    `info`, when given, receives what the script actually did with the batch —
    appended / skipped / tab from its reply. "Delivered" and "landed in the
    sheet" are different claims (the script drops duplicate links on purpose),
    and without this the difference is invisible from our side.

    follow_redirects is forced ON for this ONE call. The delivery loop's
    client is built with follow_redirects=False on purpose — a webhook
    receiver that redirects us is a receiver we should not be following — but
    an Apps Script /exec URL ALWAYS 302s to script.googleusercontent.com, so
    refusing here would mean every delivery failed on a redirect that is part
    of how the platform works.
    """
    if not exec_url:
        return False, "this target has no Apps Script URL"
    if not token:
        return False, "no token — the .env variable named by this target is unset"
    payload = {"token": token, "tab": tab or "Sheet1",
               "header": HEADER, "rows": sheet_rows(rows)}
    try:
        rep = await client.post(exec_url, json=payload, timeout=60.0,
                                follow_redirects=True)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    text = getattr(rep, "text", "") or ""
    try:
        data = rep.json()
    except Exception:
        return False, _script_hint(rep, text)
    if not isinstance(data, dict):
        return False, _script_hint(rep, text)
    if data.get("ok"):
        if isinstance(info, dict):
            for k in ("appended", "skipped", "tab"):
                if k in data:
                    info[k] = data[k]
        return True, ""
    err = str(data.get("error") or "").strip()
    if err == "bad token":
        # Worth spelling out: the two ends hold different strings, and which
        # one is wrong is not knowable from here.
        return False, ("the script rejected our token — the TOKEN line in the "
                       "script and the value in .env must match exactly, and "
                       "the script must be RE-DEPLOYED after editing it")
    return False, err or _script_hint(rep, text)


async def check_script_access(client, exec_url: str, token: str,
                              tab: str) -> tuple[bool, str]:
    """
    Prove the deployment works without putting a post in the sheet.

    An empty batch: the script still checks the token, still creates the tab
    if it is missing and still writes the header, so what this proves is
    everything except that we have rows to send.
    """
    return await via_script(client, exec_url, token, tab, [])


# --------------------------------------------------------------------------
# what the sender actually calls
# --------------------------------------------------------------------------

MODE_SCRIPT = "script"
MODE_API = "service_account"
MODES = (MODE_SCRIPT, MODE_API)


def mode_of(value) -> str:
    """
    Normalize a stored mode. Anything unrecognised means script.

    Defaulting is deliberate rather than an error: the column arrived after
    the table did, so a row written before it existed reads back NULL, and the
    mode that needs no server-side credential is the safer thing to assume.
    """
    return MODE_API if str(value or "").strip() == MODE_API else MODE_SCRIPT


async def deliver(client, target, rows: list,
                  info: dict | None = None) -> tuple[bool, str]:
    """
    One batch to one sheet target, whichever way it is set up. (ok, error).

    `target` is anything carrying sheet_mode / sheet_id / sheet_tab / url /
    token — in practice webhook.DbTarget, which is also what carries the
    cursor. Both branches send rows built by the same sheet_rows(), so the
    mode is invisible in the result. `info` (script mode only) receives the
    script's appended/skipped counts — see via_script.
    """
    tab = getattr(target, "sheet_tab", "") or "Sheet1"
    if mode_of(getattr(target, "sheet_mode", "")) == MODE_API:
        return await via_api(client, getattr(target, "sheet_id", ""), tab, rows)
    return await via_script(client, getattr(target, "url", ""),
                            getattr(target, "token", ""), tab, rows, info=info)
