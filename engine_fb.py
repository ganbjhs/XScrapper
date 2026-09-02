"""
engine_fb.py — fetch a Facebook page's newest posts via ONE warm headless
browser, logged in with a burner session.

Design settled (see RULEBOOK.md §6 Facebook, and FACEBOOK_RUNBOOK.md):
  * runs from the SERVER's own IP by default (FB_USE_PROXY=0) — the VPS has
    4 TB, so Facebook does not touch the small residential pool;
  * a mobile user-agent — the mobile layout is lighter and its posts sit in
    clean role="article" blocks (this is the variant that actually parsed);
  * ONE browser/context reused across pages so Facebook's app caches and later
    pages are cheaper;
  * image/video/font bytes blocked (URLs kept), and a hard monthly byte cap so
    it can never run away.

Session + settings come from the environment, never git:
  FB_C_USER, FB_XS            the burner account cookies
  FB_USE_PROXY=0|1            0 = server IP (default); 1 = Webshare residential
  WEBSHARE_USER/PASS/GATEWAY  used only when FB_USE_PROXY=1
  FB_MONTHLY_CAP_GB=200       the runaway guard (bytes counted in fb_meter.db)

Runs on the SERVER (needs Playwright chromium). It cannot reach Facebook from
the build sandbox, so live behaviour is confirmed by `collect_fb.py run` there.
The extractor is one function (`_EXTRACT_JS`) — the piece most likely to need a
small tune after the first real run; everything else is stable.
"""

import datetime
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path

import fb_media

# A DESKTOP user-agent, deliberately. A mobile UA makes Facebook serve the
# "WebLite/Bloks" shell — content renders, but every post is a tap-to-open
# JavaScript button with NO permalink and NO role="article", which is
# impossible to extract from (this cost us a long debugging session, see
# BLUEPRINT). The desktop site renders each post as a real role="article" with
# a real permalink link, which is what the extractor needs.
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BLOCK = {"image", "media", "font"}

# Extract posts from the rendered page — two ways, best-first:
#
#   1. JSON (primary, layout-proof). Facebook server-renders every post's data
#      into <script type="application/json"> blobs (its GraphQL/Relay store).
#      We walk those blobs and pick objects whose __typename is "Story" — a
#      STABLE discriminator that survives Facebook's constant CSS/DOM reshuffles
#      (which is what breaks visible-DOM scrapers). From each Story we read id,
#      permalink, exact time, text, author + PROFILE PICTURE, media, and the
#      reaction/comment/share counts — none of which the visible DOM gives us
#      reliably. This is how the commercial scrapers (Apify/Scrapfly) do it.
#   2. DOM (fallback). role="article" blocks, kept for the case where the JSON
#      shape shifts — so a Facebook change degrades us to the old behaviour
#      instead of to zero.
_EXTRACT_JS = r"""
async () => {
  // A stable id from a permalink. FB uses several shapes:
  //   /reel/<num>, /posts/pfbid<...>, story_fbid=<num|pfbid>, /videos/<num>,
  //   /permalink/<num>, fbid=<num>. pfbid tokens are letters+digits.
  const idFrom = (h) => {
    if (!h) return null;
    let m;
    if (m = h.match(/\/reel\/(\d+)/)) return 'reel_' + m[1];
    if (m = h.match(/(pfbid[0-9A-Za-z]+)/)) return m[1];
    if (m = h.match(/story_fbid=([0-9A-Za-z]+)/)) return m[1];
    if (m = h.match(/\/posts\/([0-9A-Za-z]+)/)) return m[1];
    if (m = h.match(/\/videos\/(\d+)/)) return 'vid_' + m[1];
    if (m = h.match(/\/permalink\/(\d+)/)) return m[1];
    if (m = h.match(/fbid=([0-9A-Za-z]+)/)) return m[1];
    return null;
  };
  const isPerma = (h) =>
    /\/posts\/|\/story|story_fbid=|\/videos\/|\/photos\/|\/permalink\/|\/reel\//.test(h);

  // ---- generic JSON walkers (depth-capped so a huge blob can't hang) ----
  const deepFind = (o, pred, d) => {
    d = d || 0;
    if (!o || typeof o !== 'object' || d > 14) return null;
    if (pred(o)) return o;
    if (Array.isArray(o)) { for (const x of o) { const r = deepFind(x, pred, d + 1); if (r) return r; } return null; }
    for (const k in o) { const r = deepFind(o[k], pred, d + 1); if (r) return r; }
    return null;
  };
  const deepAll = (o, pred, acc, d) => {
    acc = acc || []; d = d || 0;
    if (!o || typeof o !== 'object' || d > 14 || acc.length > 40) return acc;
    if (pred(o)) acc.push(o);
    if (Array.isArray(o)) { for (const x of o) deepAll(x, pred, acc, d + 1); return acc; }
    for (const k in o) deepAll(o[k], pred, acc, d + 1);
    return acc;
  };

  // ---- 1) JSON extraction ----
  const stories = [];
  const seen = new Set();
  const collect = (o, d) => {
    d = d || 0;
    if (!o || typeof o !== 'object' || d > 16) return;
    if (Array.isArray(o)) { for (const x of o) collect(x, d + 1); return; }
    if (o.__typename === 'Story' && (o.post_id || o.creation_time || o.wwwURL || o.url)) {
      const key = o.post_id || o.id || o.wwwURL || o.url;
      if (!seen.has(key)) { seen.add(key); stories.push(o); }
    }
    for (const k in o) { const v = o[k]; if (v && typeof v === 'object') collect(v, d + 1); }
  };
  for (const s of document.querySelectorAll('script[type="application/json"]')) {
    let data; try { data = JSON.parse(s.textContent); } catch (e) { continue; }
    collect(data, 0);
  }

  const jsonPosts = [];
  for (const st of stories) {
    const id = st.post_id || st.id || null;
    let perma = st.wwwURL || st.url || null;
    if (!perma) { const u = deepFind(st, (o) => typeof o.url === 'string' && isPerma(o.url)); perma = u ? u.url : null; }
    // message text: a node with a string .text and formatting ranges is the body
    const msg = (st.message && typeof st.message.text === 'string')
      ? st.message
      : deepFind(st, (o) => o && typeof o.text === 'string' && Array.isArray(o.ranges));
    const text = msg && msg.text ? msg.text : '';
    // author + profile picture
    const actor = (Array.isArray(st.actors) && st.actors[0])
      ? st.actors[0]
      : deepFind(st, (o) => o && (o.__typename === 'User' || o.__typename === 'Page') && o.name);
    const author = actor ? (actor.name || null) : null;
    const author_url = actor ? (actor.url || null) : null;
    const avatar = actor
      ? ((actor.profile_picture && actor.profile_picture.uri) || actor.profile_picture_url || null)
      : null;
    const created_ms = st.creation_time ? st.creation_time * 1000 : null;
    // media (URLs only)
    const media = [];
    for (const m of deepAll(st, (o) => o && (o.__typename === 'Photo' || o.__typename === 'Video' || o.__typename === 'GenericAttachmentMedia'))) {
      if (m.__typename === 'Video' || m.playable_url || m.playable_url_quality_hd) {
        const thumb = (m.preferred_thumbnail && m.preferred_thumbnail.image && m.preferred_thumbnail.image.uri) || (m.image && m.image.uri) || '';
        const url = m.playable_url_quality_hd || m.playable_url || thumb;
        if (url || thumb) media.push({ type: 'video', url: url || thumb, thumb: thumb || url });
      } else {
        const uri = (m.image && m.image.uri) || (m.photo_image && m.photo_image.uri) || (m.viewer_image && m.viewer_image.uri) || '';
        if (uri) media.push({ type: 'photo', url: uri, thumb: uri });
      }
      if (media.length >= 6) break;
    }
    // reaction / comment / share counts
    const fbk = deepFind(st, (o) => o && (o.reaction_count || o.i18n_reaction_count || o.comment_rendering_instance || o.share_count));
    const likes = fbk && fbk.reaction_count ? fbk.reaction_count.count : null;
    const comments = fbk
      ? ((fbk.comment_rendering_instance && fbk.comment_rendering_instance.comments && fbk.comment_rendering_instance.comments.total_count)
         || (fbk.comment_count && (fbk.comment_count.total_count || (typeof fbk.comment_count === 'number' ? fbk.comment_count : null)))
         || fbk.total_comment_count || null)
      : null;
    const shares = fbk && fbk.share_count ? fbk.share_count.count : null;
    if (!id && !perma) continue;
    jsonPosts.push({
      id: id ? String(id) : idFrom(perma),
      author, author_url, author_avatar: avatar, permalink: perma,
      text: (text || '').slice(0, 2000), created_ms, media,
      like_count: likes, comment_count: comments, share_count: shares,
    });
  }

  // ---- 2) DOM fallback ----
  //
  // Deliberately rich, not minimal. The Favorites feed lands HERE whenever the
  // graphql capture comes back empty, and the first 160 posts collected that
  // way carried no time, no counts, no author name and a body cut at "See
  // more" — a post stripped of when, who and how big is barely a post. Every
  // field below is read from the SAME article node the text came from.

  // True for an avatar, a reaction glyph or a UI sprite — NOT post media.
  // Facebook declares the rendered box in the stp/ctp/cstp params
  // (s960x960, p180x540 …); anything whose largest declared box is <= 100px is
  // chrome. Images with no declared box (full-size originals) are kept.
  const isJunkImg = (s) => {
    if (!s || !/scontent|fbcdn/.test(s)) return true;
    if (/static\.xx\.fbcdn\.net|\/emoji\.php\/|\/rsrc\.php|safe_image\.php/.test(s)) return true;
    const boxes = (s.match(/[?&](?:stp|ctp|cstp)=[^&]*/g) || []).join('&');
    const dims = boxes.match(/[sp](\d{2,4})x(\d{2,4})/g) || [];
    if (dims.length === 0) return false;
    return !dims.some((b) => { const d = b.match(/(\d{2,4})x(\d{2,4})/);
                               return d && Math.max(+d[1], +d[2]) > 100; });
  };
  // Facebook draws some images as <image> inside an <svg>, not <img>.
  const imgSrc = (el) => el.tagName === 'IMG'
    ? el.src
    : (el.getAttribute('xlink:href') || el.getAttribute('href') || '');

  // "See more" hides most of a long caption behind a local text toggle — no
  // navigation, no write, nothing posted. Expand every one, then read.
  for (const b of document.querySelectorAll('div[role="button"], span[role="button"]')) {
    const t = (b.innerText || '').trim();
    if (/^(see more|see more\.\.\.|और देखें|अधिक देखें)$/i.test(t)) {
      try { b.click(); } catch (e) {}
    }
  }
  await new Promise((r) => setTimeout(r, 700));

  const out = [];
  for (const a of document.querySelectorAll('[role="article"]')) {
    if (a.closest('[role="dialog"]')) continue;   // skip comment/reel popovers
    const anchors = [...a.querySelectorAll('a[href]')];
    const permaEl = anchors.find((l) => isPerma(l.href)) || null;
    const perma = permaEl ? permaEl.href : null;
    // Author. profile_name is Facebook's OWN label for the byline (it uses it
    // for ad rendering), so it survives the CSS reshuffles that break heading
    // selectors; the headings are kept behind it.
    const head = a.querySelector('[data-ad-rendering-role="profile_name"] a')
      || a.querySelector('h2 a, h3 a, h4 a, strong a')
      || a.querySelector('a[aria-label]');
    let author = head ? (head.innerText || '').trim().split('\n')[0] : null;
    if (author && author.length > 90) author = author.slice(0, 90);
    const author_url = head ? head.href : null;
    // The avatar is precisely the image isJunkImg rejects as too small.
    const avatarEl = [...a.querySelectorAll('img, image')]
      .find((e) => { const s = imgSrc(e);
                     return s && /scontent|fbcdn/.test(s) && isJunkImg(s); });
    const author_avatar = avatarEl ? imgSrc(avatarEl) : null;
    // Posted time. The permalink anchor carries it three ways depending on the
    // layout: an aria-label ("2 August at 10:31"), its own text ("5h"), or a
    // legacy [data-utime] epoch. Send all of them; Python decides.
    const utimeEl = a.querySelector('[data-utime]');
    const time_text = (permaEl && (permaEl.getAttribute('aria-label')
      || (permaEl.innerText || '').trim())) || null;
    const utime = utimeEl ? +utimeEl.getAttribute('data-utime') : null;
    const bodies = [...a.querySelectorAll('div[dir="auto"]')]
      .map((d) => (d.innerText || '').trim()).filter(Boolean);
    let text = bodies.sort((x, y) => y.length - x.length)[0] || "";
    text = text.replace(/[\s.…]*See more\s*$/i, '').replace(/[\s.…]*और देखें\s*$/, '');
    // Counts, as the reader sees them. The reaction total is an aria-label
    // ("1.2K reactions"); comments and shares are plain text in the bar.
    const atxt = (a.innerText || '');
    const grab = (re) => { const m = atxt.match(re); return m ? m[1] : null; };
    const reactEl = a.querySelector('[aria-label*="eaction"]');
    const counts_raw = {
      reactions: reactEl ? reactEl.getAttribute('aria-label') : null,
      comments: grab(/([\d.,]+\s*[KMkm]?)\s*comments?/i),
      shares: grab(/([\d.,]+\s*[KMkm]?)\s*shares?/i),
    };
    const imgs = [...a.querySelectorAll('img, image')].map(imgSrc)
      .filter((s) => s && !isJunkImg(s));
    out.push({ id: idFrom(perma), author, author_url, author_avatar,
               permalink: perma, time_text, utime, counts_raw,
               text: text.slice(0, 2000), media: [...new Set(imgs)].slice(0, 6) });
  }
  // Diagnostics gathered from the SAME render, so if extraction returns nothing
  // we still learn what the page held — surfaced in the "Fetch now" log, no
  // terminal needed. Returned together so it can never be skipped.
  const permaAll = [...document.querySelectorAll('a[href]')].map(a => a.href)
    .filter(isPerma);
  const roles = {};
  for (const e of document.querySelectorAll('[role]')) {
    const r = e.getAttribute('role'); roles[r] = (roles[r] || 0) + 1;
  }
  // Every link on the page (href + its short text), so the real permalink
  // shape is visible even when it does not match the patterns above.
  const allLinks = [...new Set([...document.querySelectorAll('a[href]')]
    .map(a => a.href))].slice(0, 45);
  // Climb from each long text block to the first ancestor that also holds an
  // image and links — that ancestor IS the post container on layouts without
  // role="article". Report its tag/attrs/links so the selector can be written.
  const longs = [...document.querySelectorAll('div[dir="auto"]')]
    .filter(d => (d.innerText || "").trim().length > 60).slice(0, 5);
  const containers = longs.map(d => {
    let el = d;
    for (let i = 0; i < 9 && el; i++) {
      const hasImg = el.querySelector('img[src*="fbcdn"], img[src*="scontent"]');
      const links = [...new Set([...el.querySelectorAll('a[href]')].map(a => a.href))];
      if (hasImg && links.length) {
        return { tag: el.tagName, role: el.getAttribute('role'),
                 attrs: el.getAttributeNames().slice(0, 12),
                 dataft: el.getAttribute('data-ft'),
                 links: links.slice(0, 8),
                 text: (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80) };
      }
      el = el.parentElement;
    }
    return { none: (d.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60) };
  });
  const diag = {
    url: location.href,
    title: document.title,
    json_stories: stories.length,
    json_posts: jsonPosts.length,
    articles: document.querySelectorAll('[role="article"]').length,
    feed: document.querySelectorAll('[role="feed"]').length,
    permalinks: permaAll.length,
    roles: roles,
    sample: [...new Set(permaAll)].slice(0, 5),
    all_links: allLinks,
    containers: containers,
    body_head: (document.body ? document.body.innerText : "")
      .replace(/\s+/g, " ").trim().slice(0, 240),
  };
  return { json_posts: jsonPosts, dom_posts: out, diag };
}
"""



# --------------------------------------------------------------------------
# GraphQL response parsing (the reliable path for a LOGGED-IN session)
# --------------------------------------------------------------------------
#
# When logged in, Facebook does NOT embed the feed in the page — it fetches it
# over background /graphql requests after load. So the most reliable extraction
# is to capture those responses and pull the posts out of THEM. Each post is a
# "Story" object; we read the same fields the on-page JSON would have carried
# (id, permalink, time, text, author + profile picture, media, counts). Pure
# functions so the whole thing is unit-tested offline.

_PERMA_RE = re.compile(
    r"/posts/|/story|story_fbid=|/videos/|/photos/|/permalink/|/reel/")


def _is_perma(h) -> bool:
    return bool(h and _PERMA_RE.search(h))


def _handle_from_url(u):
    """The page handle from a profile URL — 'narendramodi' from
    facebook.com/narendramodi, or the numeric id from a profile.php?id= link.
    This is how a post in a mixed feed is attributed back to its page."""
    if not u or not isinstance(u, str):
        return None
    m = re.search(r"facebook\.com/([^/?#]+)", u)
    if not m:
        return None
    seg = m.group(1)
    if seg in ("profile.php", "people", "pages", "groups", "watch", "reel"):
        m2 = re.search(r"[?&]id=(\d+)", u)
        return m2.group(1) if m2 else None
    return seg or None


# --------------------------------------------------------------------------
# Reading what the DOM shows — numbers, times, and which images are real
# --------------------------------------------------------------------------
#
# The DOM fallback returns strings a human would read ("1.2K", "5h",
# "2 August at 10:31"), not machine fields. These three functions turn them
# into the record shape, and they are pure so the tests can pin them.

_REL_RE = re.compile(
    r"^\s*(?:about\s+)?(\d+)\s*"
    r"(s|m|h|d|w|y|sec|secs|second|seconds|min|mins|minute|minutes|"
    r"hr|hrs|hour|hours|day|days|week|weeks|yr|yrs|year|years)\b", re.I)
_REL_MS = {
    "s": 1_000, "sec": 1_000, "secs": 1_000, "second": 1_000, "seconds": 1_000,
    "m": 60_000, "min": 60_000, "mins": 60_000, "minute": 60_000,
    "minutes": 60_000,
    "h": 3_600_000, "hr": 3_600_000, "hrs": 3_600_000, "hour": 3_600_000,
    "hours": 3_600_000,
    "d": 86_400_000, "day": 86_400_000, "days": 86_400_000,
    "w": 604_800_000, "week": 604_800_000, "weeks": 604_800_000,
    "y": 31_536_000_000, "yr": 31_536_000_000, "yrs": 31_536_000_000,
    "year": 31_536_000_000, "years": 31_536_000_000,
}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _time_ms(text, utime=None, now_ms=None):
    """Posted time in epoch ms from whatever the post's timestamp link showed.

    Order of trust: an exact [data-utime] epoch, then a relative age ("5h"),
    then "Yesterday", then an absolute date ("2 August at 10:31"). The absolute
    branch has NO timezone — the browser renders in the account's, and we
    cannot know it here — so its result is honest to the day and approximate
    within it. Everything is clamped to `now`: a post cannot have been made
    after it was collected, and a future created_ms would poison lag_ms.
    Unreadable input returns None, which stays None. A missing time is a
    state; a guessed one is a lie."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if utime:
        try:
            u = int(utime)
            ms = u * 1000 if u < 10_000_000_000 else u
            return min(ms, now)
        except (TypeError, ValueError):
            pass
    if not text:
        return None
    t = str(text).strip()
    if re.search(r"just now|abhi", t, re.I):
        return now
    m = _REL_RE.match(t)
    if m:
        step = _REL_MS.get(m.group(2).lower())
        if step:
            return min(now - int(m.group(1)) * step, now)

    tm = re.search(r"(\d{1,2}):(\d{2})", t)
    hh, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (12, 0)
    if tm and re.search(r"\bp\.?\s?m\.?", t, re.I) and hh < 12:
        hh += 12
    if tm and re.search(r"\ba\.?\s?m\.?", t, re.I) and hh == 12:
        hh = 0
    base = datetime.datetime.utcfromtimestamp(now / 1000)
    if re.search(r"yesterday", t, re.I):
        d = base - datetime.timedelta(days=1)
        return min(int(datetime.datetime(d.year, d.month, d.day, hh, mi)
                       .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000), now)
    if re.search(r"\btoday\b", t, re.I):
        return min(int(datetime.datetime(base.year, base.month, base.day, hh, mi)
                       .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000), now)
    dm = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})|([A-Za-z]{3,})\s+(\d{1,2})", t)
    if not dm:
        return None
    day = int(dm.group(1) or dm.group(4))
    mon = _MONTHS.get((dm.group(2) or dm.group(3) or "")[:3].lower())
    if not mon or not (1 <= day <= 31):
        return None
    ym = re.search(r"\b(20\d{2})\b", t)
    year = int(ym.group(1)) if ym else base.year
    try:
        dt = datetime.datetime(year, mon, day, hh, mi,
                               tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    ms = int(dt.timestamp() * 1000)
    # No year shown and the date reads as future => it belongs to last year.
    if not ym and ms > now + 2 * 86_400_000:
        try:
            ms = int(dt.replace(year=year - 1).timestamp() * 1000)
        except ValueError:
            return None
    return min(ms, now)


_JUNK_IMG = re.compile(
    r"static\.xx\.fbcdn\.net|/emoji\.php/|/rsrc\.php|safe_image\.php")
_BOX_PARAM = re.compile(r"[?&](?:stp|ctp|cstp)=[^&]*")
_BOX_DIM = re.compile(r"[sp](\d{2,4})x(\d{2,4})")
_VIDEO_PERMA = re.compile(r"/reel/|/videos/|/watch")


def _is_post_image(u) -> bool:
    """Is this URL the post's OWN picture, or page chrome?

    Facebook declares the rendered box in the stp/ctp/cstp params. An avatar is
    p32x32, a reaction glyph is s16x16, the post photo is s960x960 — so the
    largest declared box separates them without a network call. The emoji
    sprite that turned up as a 'photo' in the first 160 posts
    (static.xx.fbcdn.net/images/emoji.php/...) is excluded by host."""
    if not u or not isinstance(u, str):
        return False
    if not re.search(r"scontent|fbcdn", u):
        return False
    if _JUNK_IMG.search(u):
        return False
    dims = _BOX_DIM.findall("".join(_BOX_PARAM.findall(u)))
    if not dims:
        return True                       # no declared box = a full original
    return any(max(int(a), int(b)) > 100 for a, b in dims)


def _clean_media(media, permalink=None, avatar=None, limit=6):
    """Normalize to [{type,url,thumb}], dropping chrome and duplicates.

    Accepts both shapes the extractor produces: {type,url,thumb} dicts from the
    JSON/graphql paths and bare URL strings from the DOM path. A post whose
    permalink is a reel or a video is a VIDEO however its thumbnail was found —
    the old code labelled those 'photo', so the feed offered a still and called
    it the post."""
    is_video = bool(permalink and _VIDEO_PERMA.search(permalink))
    out, seen = [], set()
    for m in (media or []):
        if isinstance(m, str):
            m = {"type": "photo", "url": m, "thumb": m}
        if not isinstance(m, dict):
            continue
        url = (m.get("url") or m.get("thumb") or "").strip()
        if not url or url in seen:
            continue
        kind = m.get("type") or "photo"
        if kind == "photo":
            if not _is_post_image(url) or (avatar and url == avatar):
                continue
            if is_video:
                kind = "video"
        seen.add(url)
        item = {"type": kind, "url": url, "thumb": m.get("thumb") or url}
        if m.get("duration"):
            item["duration"] = m["duration"]
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _iter_json_objects(blob: str):
    """Facebook streams graphql as one JSON object, or several concatenated /
    newline-delimited. Yield every object we can parse out of the blob."""
    if not blob:
        return
    b = blob.strip()
    if b.startswith("for (;;);"):
        b = b[len("for (;;);"):]
    try:
        yield json.loads(b)
        return
    except Exception:
        pass
    for line in b.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _deep_find(o, pred, depth=0):
    if depth > 16 or not isinstance(o, (dict, list)):
        return None
    if isinstance(o, dict):
        if pred(o):
            return o
        for v in o.values():
            r = _deep_find(v, pred, depth + 1)
            if r is not None:
                return r
    else:
        for x in o:
            r = _deep_find(x, pred, depth + 1)
            if r is not None:
                return r
    return None


def _deep_all(o, pred, acc=None, depth=0):
    if acc is None:
        acc = []
    if depth > 16 or len(acc) > 40 or not isinstance(o, (dict, list)):
        return acc
    if isinstance(o, dict):
        if pred(o):
            acc.append(o)
        for v in o.values():
            _deep_all(v, pred, acc, depth + 1)
    else:
        for x in o:
            _deep_all(x, pred, acc, depth + 1)
    return acc


_ABBREV_RE = re.compile(r"^\s*([\d.]+)\s*([KkMmBb]?)\s*$")
_LOOSE_NUM_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*([KkMmBb]?)")

# Any of these keys on a node marks it as feedback-ish (carrying engagement
# counts). Facebook ships the counts under several names depending on which
# GraphQL query produced the payload — read them all.
_COUNT_KEYS = ("reaction_count", "i18n_reaction_count", "reactors",
               "comment_rendering_instance", "comment_count",
               "total_comment_count", "share_count", "reshares")


def _num(v):
    """A count that may arrive as int, digit string, or '1.2K' / '3,4 mn' style
    abbreviation. None when it can't be read as a number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace(" ", "")
        m = _ABBREV_RE.match(s)
        if not m:
            # Not the whole string. The DOM path reads counts as a person sees
            # them — "1.2K reactions", "All reactions: 47", "83 comments" — so
            # fall back to the first number IN the label rather than dropping a
            # count Facebook plainly displayed.
            m = _LOOSE_NUM_RE.search(v)
        if m:
            try:
                return int(float(m.group(1).replace(",", "")) *
                           {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2).lower(), 1))
            except ValueError:
                return None
    return None


def _counts_from(o):
    """(likes, comments, shares) out of one feedback-ish node."""
    if not isinstance(o, dict):
        return None, None, None
    rc = o.get("reaction_count")
    likes = _num(rc.get("count")) if isinstance(rc, dict) else _num(rc)
    if likes is None and isinstance(o.get("reactors"), dict):
        likes = _num(o["reactors"].get("count"))
    if likes is None:
        likes = _num(o.get("i18n_reaction_count"))
    cri = o.get("comment_rendering_instance") or {}
    comments = _num(((cri.get("comments") or {}).get("total_count"))) \
        if isinstance(cri, dict) else None
    if comments is None:
        cc = o.get("comment_count")
        comments = _num(cc.get("total_count")) if isinstance(cc, dict) else _num(cc)
    if comments is None:
        comments = _num(o.get("total_comment_count"))
    sc = o.get("share_count")
    shares = _num(sc.get("count")) if isinstance(sc, dict) else _num(sc)
    if shares is None and isinstance(o.get("reshares"), dict):
        shares = _num(o["reshares"].get("count"))
    return likes, comments, shares


def _merge_counts(base, node):
    """Fill the None slots of (likes, comments, shares) from one more node."""
    l, c, s = _counts_from(node)
    return (base[0] if base[0] is not None else l,
            base[1] if base[1] is not None else c,
            base[2] if base[2] is not None else s)


def _feedback_map(blobs):
    """
    Engagement counts often arrive in SEPARATE graphql payloads from the Story
    itself (the CometUFI queries). Walk every blob for feedback-ish nodes and
    map them by every id they carry — the Feedback id and, crucially,
    subscription_target_id, which IS the numeric post id — so counts can be
    stitched back onto the right post afterwards.
    """
    fmap = {}

    def walk(o, depth=0):
        if depth > 18 or not isinstance(o, (dict, list)):
            return
        if isinstance(o, list):
            for x in o:
                walk(x, depth + 1)
            return
        if any(k in o for k in _COUNT_KEYS):
            counts = _counts_from(o)
            if any(v is not None for v in counts):
                for key in (o.get("subscription_target_id"), o.get("id"),
                            o.get("legacy_fbid")):
                    if key:
                        prev = fmap.get(str(key), (None, None, None))
                        fmap[str(key)] = (
                            prev[0] if prev[0] is not None else counts[0],
                            prev[1] if prev[1] is not None else counts[1],
                            prev[2] if prev[2] is not None else counts[2])
        for v in o.values():
            if isinstance(v, (dict, list)):
                walk(v, depth + 1)

    for b in blobs or []:
        for obj in _iter_json_objects(b):
            walk(obj)
    return fmap


def _walk_stories(o, out, seen, depth=0):
    if depth > 18 or not isinstance(o, (dict, list)):
        return
    if isinstance(o, list):
        for x in o:
            _walk_stories(x, out, seen, depth + 1)
        return
    if o.get("__typename") == "Story" and (
            o.get("post_id") or o.get("creation_time") or o.get("wwwURL")):
        key = o.get("post_id") or o.get("id") or o.get("wwwURL")
        if key not in seen:
            seen.add(key)
            out.append(o)
    for v in o.values():
        if isinstance(v, (dict, list)):
            _walk_stories(v, out, seen, depth + 1)


def _story_to_post(st: dict):
    pid = st.get("post_id") or st.get("id")
    perma = st.get("wwwURL") or st.get("url")
    if not perma:
        u = _deep_find(st, lambda o: isinstance(o.get("url"), str) and _is_perma(o["url"]))
        perma = u["url"] if u else None
    if not pid and not perma:
        return None
    msg = st.get("message") if isinstance(st.get("message"), dict) and st["message"].get("text") \
        else _deep_find(st, lambda o: isinstance(o.get("text"), str) and isinstance(o.get("ranges"), list))
    text = (msg or {}).get("text") or ""
    actors = st.get("actors")
    actor = actors[0] if isinstance(actors, list) and actors else \
        _deep_find(st, lambda o: o.get("__typename") in ("User", "Page") and o.get("name"))
    author = actor.get("name") if actor else None
    avatar = None
    author_handle = None
    if actor:
        pp = actor.get("profile_picture")
        avatar = (pp.get("uri") if isinstance(pp, dict) else None) or actor.get("profile_picture_url")
        author_handle = actor.get("username") or _handle_from_url(actor.get("url")) \
            or (str(actor.get("id")) if actor.get("id") else None)
    created_ms = st["creation_time"] * 1000 if st.get("creation_time") else None
    media = []
    for m in _deep_all(st, lambda o: o.get("__typename") in ("Photo", "Video", "GenericAttachmentMedia")):
        if m.get("__typename") == "Video" or m.get("playable_url") or m.get("playable_url_quality_hd"):
            thumb = ((m.get("preferred_thumbnail") or {}).get("image") or {}).get("uri") \
                or (m.get("image") or {}).get("uri") or ""
            url = m.get("playable_url_quality_hd") or m.get("playable_url") or thumb
            if url or thumb:
                media.append({"type": "video", "url": url or thumb, "thumb": thumb or url})
        else:
            uri = (m.get("image") or {}).get("uri") or (m.get("photo_image") or {}).get("uri") or ""
            if uri:
                media.append({"type": "photo", "url": uri, "thumb": uri})
        if len(media) >= 6:
            break
    # Counts: merge every feedback-ish node inside the story (Facebook splits
    # reactions / comments / shares across nested nodes), starting with the
    # story's own feedback object.
    counts = (None, None, None)
    counts = _merge_counts(counts, st.get("feedback"))
    for node in _deep_all(st, lambda o: any(k in o for k in _COUNT_KEYS)):
        counts = _merge_counts(counts, node)
        if all(v is not None for v in counts):
            break
    likes, comments, shares = counts
    # Every id the story's feedback carries, so counts that arrived in a
    # SEPARATE graphql payload can be stitched on afterwards (_feedback_map).
    fb_ids = []
    fbnode = st.get("feedback")
    if isinstance(fbnode, dict):
        for k in ("id", "subscription_target_id", "legacy_fbid"):
            if fbnode.get(k):
                fb_ids.append(str(fbnode[k]))
    return {"id": str(pid) if pid else _fallback_id(perma), "author": author,
            "author_handle": author_handle, "author_avatar": avatar,
            "permalink": perma, "text": text[:2000],
            "created_ms": created_ms,
            "media": _clean_media(media, perma, avatar),
            "like_count": likes, "comment_count": comments, "share_count": shares,
            "feedback_ids": fb_ids}


def _stories_from_graphql(blobs) -> list:
    out, seen = [], set()
    for b in blobs or []:
        for obj in _iter_json_objects(b):
            _walk_stories(obj, out, seen)
    posts = []
    for s in out:
        p = _story_to_post(s)
        if p:
            posts.append(p)
    # Second pass: counts that came in SEPARATE payloads (the UFI queries) are
    # matched back by post id / feedback id and fill any still-empty slots.
    if posts:
        fmap = _feedback_map(blobs)
        if fmap:
            for p in posts:
                keys = [str(p.get("id") or "")] + list(p.get("feedback_ids") or [])
                for k in keys:
                    hit = fmap.get(k)
                    if not hit:
                        continue
                    if p.get("like_count") is None:
                        p["like_count"] = hit[0]
                    if p.get("comment_count") is None:
                        p["comment_count"] = hit[1]
                    if p.get("share_count") is None:
                        p["share_count"] = hit[2]
    for p in posts:
        p.pop("feedback_ids", None)
    return posts


# --------------------------------------------------------------------------
# Login health — the circuit breaker (RULEBOOK §6: one attempt, then a human)
# --------------------------------------------------------------------------
#
# Facebook walls that need a HUMAN (a checkpoint / identity verification — it
# throws these even when 2FA is off, and no script may answer one) must not be
# hammered: automatic retries in a loop are exactly what gets the burner
# account locked for good. So the engine keeps a tiny health file: after ONE
# failed login attempt it records the actual cause and BLOCKS further attempts
# until an operator clears it from the dashboard (Watchlists → Facebook pages
# → "Clear & retry"). Collectors check it before even opening a browser, so a
# blocked login costs nothing per tick.

def _health_path():
    return os.getenv("FB_HEALTH_PATH", "fb_health.json")


def read_health() -> dict:
    try:
        with open(_health_path()) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_health(**kw):
    h = read_health()
    h.update(kw)
    try:
        with open(_health_path(), "w") as f:
            json.dump(h, f)
    except Exception:
        pass


def login_blocked():
    """The health dict when login is blocked pending a human, else None."""
    h = read_health()
    return h if h.get("blocked") else None


def clear_login_block():
    """Operator action: forget the block so the NEXT run may try one login."""
    try:
        os.remove(_health_path())
    except FileNotFoundError:
        pass


def classify_login_wall(url: str, email: str = "") -> tuple:
    """(reason, detail) for a login that did not complete, from the URL we
    landed on. The detail is written for the operator, not for a log grepper."""
    u = (url or "").lower()
    who = email or "the collector account"
    if ("checkpoint" in u or "two_step" in u or "two_factor" in u
            or "auth_platform" in u or "/recover/" in u):
        return ("checkpoint",
                f"Facebook is holding {who} at a verification checkpoint. It "
                f"does this even when 2FA is OFF — it is an identity check, "
                f"and no script may answer it. Open facebook.com in a normal "
                f"browser, log in as {who}, complete the check, then press "
                f"\"Clear & retry\" here. If the session still fails, use "
                f"\"Reset session\" to force a fresh login.")
    return ("login_failed",
            f"Facebook did not accept the login for {who} — wrong password, "
            f"or it is blocking logins from this server IP. Verify the "
            f"credentials by logging in once from a normal browser, fix "
            f".env if needed, then press \"Clear & retry\".")


def _bandwidth_ok(meter_db, cap_bytes):
    month = time.strftime("%Y-%m", time.gmtime())
    con = sqlite3.connect(meter_db)
    con.execute("CREATE TABLE IF NOT EXISTS bw(month TEXT PRIMARY KEY, bytes INTEGER)")
    row = con.execute("SELECT bytes FROM bw WHERE month=?", (month,)).fetchone()
    con.close()
    used = row[0] if row else 0
    return used < cap_bytes, used


def _record_bytes(meter_db, n):
    month = time.strftime("%Y-%m", time.gmtime())
    con = sqlite3.connect(meter_db)
    con.execute("CREATE TABLE IF NOT EXISTS bw(month TEXT PRIMARY KEY, bytes INTEGER)")
    con.execute("INSERT INTO bw(month,bytes) VALUES(?,?) "
                "ON CONFLICT(month) DO UPDATE SET bytes=bytes+excluded.bytes",
                (month, int(n)))
    con.commit(); con.close()


class FacebookEngine:
    """One warm browser for the whole run. Use as an async context manager."""

    def __init__(self, meter_db="fb_meter.db", log=print):
        self.log = log
        self.meter_db = meter_db
        self.cap_bytes = int(float(os.getenv("FB_MONTHLY_CAP_GB", "200")) * 1e9)
        # Where the whole logged-in session (cookies incl. the browser's OWN
        # datr) is saved between runs, so the session is reused instead of
        # replayed cold every time — the reuse is what stops the logouts.
        self.state_path = os.getenv("FB_STATE_PATH", "fb_state.json")
        self.email = os.getenv("FB_EMAIL", "").strip()
        self.password = os.getenv("FB_PASSWORD", "")
        self.on_favorites = False   # set by fetch_favorites: did we reach the feed
        # The media store sits beside the meter db — same install directory,
        # no new configuration to get wrong. FB_MEDIA=0 turns caching off.
        self.media_root = Path(self.meter_db).resolve().parent
        self.cache_media = os.getenv("FB_MEDIA", "1") != "0"
        self._pw = self._browser = self._ctx = self._page = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        use_proxy = os.getenv("FB_USE_PROXY", "0") == "1"
        proxy = None
        if use_proxy:
            gw = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
            proxy = {"server": f"http://{gw}",
                     "username": os.getenv("WEBSHARE_USER", ""),
                     "password": os.getenv("WEBSHARE_PASS", "")}
        self._browser = await self._pw.chromium.launch(
            headless=True, proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  # Strip the biggest "I'm automated" tell — the Automation
                  # Controlled blink feature that sets navigator.webdriver.
                  "--disable-blink-features=AutomationControlled"])

        # Prefer a saved session (it carries the browser's own datr, so Facebook
        # keeps it logged in). Fall back to the raw cookies from .env on first
        # run; the login-with-password path below rebuilds the session if both
        # are stale.
        ctx_kw = dict(user_agent=DESKTOP_UA,
                      viewport={"width": 1366, "height": 2600}, locale="en-US",
                      timezone_id=os.getenv("FB_TIMEZONE", "Asia/Kolkata"))
        if os.path.exists(self.state_path):
            ctx_kw["storage_state"] = self.state_path
            self._ctx = await self._browser.new_context(**ctx_kw)
            self.log(f"[fb] reusing saved session {self.state_path}")
        else:
            self._ctx = await self._browser.new_context(**ctx_kw)
            cookies = []
            if os.getenv("FB_C_USER") and os.getenv("FB_XS"):
                cookies = [
                    {"name": "c_user", "value": os.getenv("FB_C_USER", ""),
                     "domain": ".facebook.com", "path": "/"},
                    {"name": "xs", "value": os.getenv("FB_XS", ""),
                     "domain": ".facebook.com", "path": "/"}]
                for name, env in (("datr", "FB_DATR"), ("sb", "FB_SB")):
                    val = os.getenv(env, "")
                    if val:
                        cookies.append({"name": name, "value": val,
                                        "domain": ".facebook.com", "path": "/"})
            if cookies:
                await self._ctx.add_cookies(cookies)

        # Stealth: erase the leftover automation fingerprints a headless Chrome
        # still carries, so the browser reads as an ordinary logged-in Chrome.
        await self._ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = window.chrome || { runtime: {} };
        """)

        async def route(r):
            await (r.abort() if r.request.resource_type in BLOCK else r.continue_())
        await self._ctx.route("**/*", route)

        self._bytes = 0
        self._gql = []          # captured graphql response bodies (per attempt)
        self._gql_bytes = 0

        async def on_resp(resp):
            try:
                cl = resp.headers.get("content-length")
                if cl:
                    self._bytes += int(cl)
            except Exception:
                pass
            # Capture the graphql responses that actually carry posts — this is
            # where a logged-in session's feed data lives (not in the page).
            try:
                if "/graphql" in resp.url and self._gql_bytes < 14_000_000:
                    body = await resp.text()
                    # Count graphql payloads toward the bandwidth meter — they
                    # are usually chunked (no content-length), so the header
                    # counter above misses them, and they are the big spend.
                    if not resp.headers.get("content-length"):
                        self._bytes += len(body)
                    if ("post_id" in body or "Story" in body
                            or "creation_time" in body):
                        self._gql.append(body)
                        self._gql_bytes += len(body)
            except Exception:
                pass
        self._ctx.on("response", on_resp)
        self._page = await self._ctx.new_page()
        return self

    async def __aexit__(self, *exc):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _open_favorites(self) -> bool:
        """
        Navigate from home to the FAVORITES feed the way a person does, trying
        several routes, and confirm success by the URL carrying filter=favorites.
        Returns True only when we are actually on the Favorites feed — the caller
        uses that to avoid mistaking the home feed for favorites.
        """
        p = self._page

        def _there():
            return "filter=favorites" in (p.url or "").lower()

        async def _try_click(fn):
            try:
                await fn()
                await p.wait_for_timeout(4000)
                return _there()
            except Exception:
                return False

        # 1) the sidebar link by href / aria-label
        for sel in ('a[href*="filter=favorites"]',
                    'a[aria-label*="Favourite" i]', 'a[aria-label*="Favorite" i]'):
            el = None
            try:
                el = await p.query_selector(sel)
            except Exception:
                el = None
            if el and await _try_click(el.click):
                return True
        # 2) role=link by visible name (UK + US spelling)
        for name in ("Favourites", "Favorites"):
            if await _try_click(
                    p.get_by_role("link", name=name, exact=False).first.click):
                return True
        # 3) open the Feeds section first, then Favourites
        try:
            await p.get_by_role("link", name="Feeds", exact=False).first.click(timeout=4000)
            await p.wait_for_timeout(3000)
        except Exception:
            pass
        for name in ("Favourites", "Favorites"):
            if await _try_click(
                    p.get_by_role("link", name=name, exact=False).first.click):
                return True
        # 4) last resort — plain text
        for name in ("Favourites", "Favorites"):
            if await _try_click(p.get_by_text(name, exact=True).first.click):
                return True
        return _there()

    async def _human_scroll(self, rounds):
        """
        Scroll like a person, not a robot: real wheel events at the cursor
        (which scroll Facebook's inner feed container — window.scrollBy does
        not), varied distances, uneven pauses, and the occasional scroll back
        up. This both looks human (fewer logouts) and actually drives the feed's
        "load more" that fires the graphql we capture.
        """
        try:
            await self._page.mouse.move(683, 500)
        except Exception:
            pass
        for _ in range(max(0, rounds)):
            try:
                await self._page.mouse.wheel(0, random.randint(500, 1500))
            except Exception:
                await self._page.evaluate("window.scrollBy(0, 900)")
            await self._page.wait_for_timeout(random.randint(1300, 3800))
            if random.random() < 0.18:      # glance back up, like a human
                try:
                    await self._page.mouse.wheel(0, -random.randint(150, 450))
                except Exception:
                    pass
                await self._page.wait_for_timeout(random.randint(500, 1400))

    async def _save_state(self):
        """Persist the whole logged-in session so the next run reuses it."""
        try:
            await self._ctx.storage_state(path=self.state_path)
        except Exception as e:
            self.log(f"[fb] could not save session: {type(e).__name__}: {e}")

    @staticmethod
    def _is_login_wall(url: str) -> bool:
        u = url or ""
        return ("login" in u or "/?next=" in u or "checkpoint" in u
                or "/recover/" in u)

    async def _login(self) -> bool:
        """
        Log in with FB_EMAIL / FB_PASSWORD in the real browser, so Facebook
        issues a fresh session bound to THIS browser's datr, and save it. This
        is the durable fix for the session getting logged out: instead of
        replaying borrowed cookies, we hold a session the browser itself owns.

        ONE attempt, then the circuit breaker: a failure records its cause in
        fb_health.json and blocks every further attempt until an operator
        clears it from the dashboard. Retrying a checkpoint in a loop is how
        accounts get locked — a blocked login is a task for a human, not for
        this function.
        """
        blk = login_blocked()
        if blk:
            self.log(f"[fb] login is BLOCKED ({blk.get('reason')}) — not "
                     f"retrying. Fix it and press \"Clear & retry\" in the "
                     f"dashboard (Watchlists → Facebook pages).")
            return False
        if not (self.email and self.password):
            self.log("[fb] logged out and no FB_EMAIL / FB_PASSWORD set — "
                     "cannot re-login")
            return False
        p = self._page
        try:
            await p.goto("https://www.facebook.com/login.php",
                         wait_until="domcontentloaded", timeout=60000)
            await p.wait_for_timeout(2500)
            await p.fill('input[name="email"]', self.email)
            await p.fill('input[name="pass"]', self.password)
            # The button's name/shape varies; try the common ones in order.
            for sel in ('button[name="login"]', 'input[name="login"]',
                        'button[type="submit"]', '[data-testid="royal_login_button"]'):
                el = await p.query_selector(sel)
                if el:
                    await el.click()
                    break
            await p.wait_for_timeout(7000)
        except Exception as e:
            self.log(f"[fb] login attempt failed: {type(e).__name__}: {e}")
            return False
        if self._is_login_wall(p.url):
            reason, detail = classify_login_wall(p.url, self.email)
            write_health(blocked=True, reason=reason, detail=detail,
                         url=p.url, ts=int(time.time()), email=self.email)
            self.log(f"[fb] login did not complete — {reason}. AUTOMATIC "
                     f"RETRIES STOPPED until an operator clears it in the "
                     f"dashboard. {detail}")
            return False
        write_health(blocked=False, reason=None, detail=None,
                     last_login=int(time.time()), ts=int(time.time()))
        await self._save_state()
        self.log("[fb] logged in with password; session saved")
        return True

    async def _cache_media(self, posts):
        """Download each post's pictures NOW, while their signatures are alive.

        This is the whole answer to expiring media, and it has to happen here —
        inside the run that found the post — because an fbcdn URL is signed and
        dies about five days later (`oe=<hex epoch>` in the URL says exactly
        when). Fetching later means fetching nothing.

        The bytes come through `self._ctx.request`, which shares the browser's
        cookies but bypasses the route handler that blocks image loading during
        the page render — so the page stays cheap and only the pictures we
        actually keep are paid for. Every byte is added to `self._bytes`, which
        is what the monthly cap counts, so this cannot escape the cap.

        Each item keeps `src`: the original Facebook URL the bytes came from.
        It is dead within the week, and that is fine — it is provenance, not a
        fallback. A video keeps its `url` (the playable stream) and gets a
        cached `thumb`; a photo has both rewritten to the stored copy.

        A failure here NEVER fails the run: the post is still worth having with
        an expiring link, which is what it had before this existed."""
        if not posts or not self.cache_media:
            return posts
        try:
            store = fb_media.MediaStore(self.media_root)
        except Exception as e:
            self.log(f"[fb] media store unavailable ({type(e).__name__}: {e}) — "
                     f"keeping Facebook's own links")
            return posts
        got = missed = 0
        for post in posts:
            for m in (post.get("media") or []):
                is_video = m.get("type") == "video"
                src = (m.get("thumb") if is_video
                       else (m.get("url") or m.get("thumb"))) or ""
                if not src.startswith("http") or fb_media.URL_PREFIX in src:
                    continue
                local = await self._fetch_image(store, src)
                if not local:
                    missed += 1
                    continue
                m["src"] = src
                m["thumb"] = local
                if not is_video:
                    m["url"] = local
                got += 1
        if got or missed:
            self.log(f"[fb] media cached: {got} stored, {missed} failed, "
                     f"{store.total_bytes()//1024} KB held")
        evicted = store.sweep()
        if evicted:
            self.log(f"[fb] media sweep: evicted {evicted} oldest files "
                     f"(FB_MEDIA_CAP_GB={os.getenv('FB_MEDIA_CAP_GB')})")
        return posts

    async def _fetch_image(self, store, url):
        """One image, or None. Never raises — see _cache_media."""
        try:
            resp = await self._ctx.request.get(url, timeout=20000)
            if not resp.ok:
                return None
            body = await resp.body()
            self._bytes += len(body)
            return store.put(body, resp.headers.get("content-type", ""), url)
        except Exception:
            return None

    def _build_from_json(self, handle, items):
        """Records from the JSON path — a real post_id is enough (no permalink
        needed); synthesize a URL if Facebook didn't give one."""
        # Canonicalize the handle to lowercase so the SAME post gets the SAME
        # post_id whether it came in via the per-page path or the mixed feed
        # path (Facebook handles are case-insensitive).
        hl = (handle or "").lower()
        posts = []
        for r in items:
            pid = r.get("id") or (r.get("permalink") and _fallback_id(r["permalink"]))
            if not pid:
                continue
            url = r.get("permalink") or f"https://www.facebook.com/{hl}/posts/{pid}"
            posts.append({
                "post_id": f"{hl}:{pid}",
                "page": hl,
                "url": url,
                "created_ms": r.get("created_ms"),
                "author_name": r.get("author"),
                "author_avatar": r.get("author_avatar"),
                "text": r.get("text") or "",
                "like_count": r.get("like_count"),
                "comment_count": r.get("comment_count"),
                "share_count": r.get("share_count"),
                "media": _clean_media(r.get("media"), url, r.get("author_avatar")),
            })
        return posts

    def _build_feed(self, items):
        """Records from a MIXED feed (Favorites) — each post is attributed to
        its OWN author page, not a single handle. Media is normalized whether it
        arrived as {type,url,thumb} dicts (JSON paths) or bare url strings (DOM)."""
        posts = []
        for r in items:
            # A post whose byline link is missing is still attributable — its
            # own permalink names the page. Dropping it was silent data loss.
            h = (r.get("author_handle")
                 or _handle_from_url(r.get("author_url"))
                 or _handle_from_url(r.get("permalink")))
            if not h:
                continue
            h = h.lower()      # canonical, case-insensitive (matches _build_from_json)
            pid = r.get("id") or (r.get("permalink") and _fallback_id(r["permalink"]))
            if not pid:
                continue
            url = r.get("permalink") or f"https://www.facebook.com/{h}/posts/{pid}"
            # The DOM path reaches here too (Favorites falls back to it), so
            # read its human-shaped fields when the JSON fields are absent.
            counts = r.get("counts_raw") or {}
            posts.append({
                "post_id": f"{h}:{pid}",
                "page": h,
                "url": url,
                "created_ms": r.get("created_ms")
                or _time_ms(r.get("time_text"), r.get("utime")),
                "author_name": r.get("author"),
                "author_avatar": r.get("author_avatar"),
                "text": r.get("text") or "",
                "like_count": r.get("like_count")
                if r.get("like_count") is not None else _num(counts.get("reactions")),
                "comment_count": r.get("comment_count")
                if r.get("comment_count") is not None else _num(counts.get("comments")),
                "share_count": r.get("share_count")
                if r.get("share_count") is not None else _num(counts.get("shares")),
                "media": _clean_media(r.get("media"), url, r.get("author_avatar")),
            })
        return posts

    def _build_posts(self, handle, raw):
        """Records from the DOM fallback — permalink required as the id source."""
        posts = []
        for r in raw:
            if not r.get("permalink"):      # drop comments / non-post articles
                continue
            pid = r.get("id") or _fallback_id(r["permalink"])
            if not pid:
                continue
            counts = r.get("counts_raw") or {}
            posts.append({
                "post_id": f"{handle}:{pid}",
                "page": handle,
                "url": r["permalink"],
                # The DOM shows the time as a human reads it ("5h",
                # "2 August at 10:31") — approximate, but a real fact, and the
                # feed sorts and ages on it.
                "created_ms": _time_ms(r.get("time_text"), r.get("utime")),
                "author_name": r.get("author"),
                "author_avatar": r.get("author_avatar"),
                "text": r.get("text") or "",
                "like_count": _num(counts.get("reactions")),
                "comment_count": _num(counts.get("comments")),
                "share_count": _num(counts.get("shares")),
                "media": _clean_media(r.get("media"), r["permalink"],
                                      r.get("author_avatar")),
            })
        return posts

    async def _attempt(self, url, handle, max_scroll, allow_login):
        """One navigate+extract. Returns (posts, diag, method). Order of trust:
        captured graphql (logged-in feed data) > on-page JSON > DOM."""
        self._gql = []          # only THIS attempt's graphql responses
        self._gql_bytes = 0
        await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2500)
        if allow_login and self._is_login_wall(self._page.url):
            self.log(f"[fb] {handle}: session logged out — attempting re-login")
            if await self._login():
                await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await self._page.wait_for_timeout(2500)
            if self._is_login_wall(self._page.url):
                self.log(f"[fb] {handle}: NOT LOGGED IN — re-login failed. "
                         f"url={self._page.url}")
                return [], None, "none"
        try:
            await self._page.wait_for_selector('[role="article"]', timeout=20000)
        except Exception:
            pass
        await self._page.wait_for_timeout(random.randint(1500, 3000))
        await self._human_scroll(max_scroll)
        res = await self._page.evaluate(_EXTRACT_JS)
        if not isinstance(res, dict):
            res = {}
        diag = res.get("diag") or {}
        # 1) captured graphql (most reliable when logged in)
        gql = _stories_from_graphql(self._gql)
        diag["gql_responses"] = len(self._gql)
        diag["gql_posts"] = len(gql)
        if gql:
            return self._build_from_json(handle, gql), diag, "gql"
        # 2) on-page JSON
        jsonp = res.get("json_posts") or []
        if jsonp:
            return self._build_from_json(handle, jsonp), diag, "json"
        # 3) DOM fallback
        return self._build_posts(handle, res.get("dom_posts") or []), diag, "dom"

    async def fetch_page(self, handle: str, max_scroll: int = 8) -> list:
        """
        Newest posts of one Facebook page, as normalized records. Refuses if the
        monthly byte cap is spent (returns [] and logs), so it can never overrun.

        Desktop site ONLY. mbasic was tested and REMOVED (RULEBOOK §6): it
        serves the WebLite shell — no post JSON, no permalinks, nothing our
        extraction needs — so falling back to it only wasted a request and
        muddied the diagnostics. Zero posts here means diagnose (the diag log),
        not degrade.
        """
        ok, used = _bandwidth_ok(self.meter_db, self.cap_bytes)
        if not ok:
            self.log(f"[fb] monthly bandwidth cap reached ({used/1e9:.1f} GB) — "
                     f"skipping {handle}")
            return []

        self._bytes = 0
        posts, diag, source = [], None, "www"
        try:
            posts, diag, method = await self._attempt(
                f"https://www.facebook.com/{handle}", handle, max_scroll,
                allow_login=True)
            source = f"www:{method}"
        except Exception as e:
            self.log(f"[fb] fetch {handle} failed: {type(e).__name__}: {e}")

        posts = await self._cache_media(posts)
        _record_bytes(self.meter_db, self._bytes)
        await self._save_state()

        self.log(f"[fb] {handle}: {len(posts)} posts via {source}, "
                 f"{self._bytes//1024} KB")
        if diag is not None:
            diag["handle"] = handle
            diag["parsed"] = len(posts)
            try:
                with open(os.getenv("FB_DIAG_PATH", "fb_diag.json"), "w") as f:
                    json.dump(diag, f)
            except Exception:
                pass
            self.log(f"[fb] {handle}: diag — title={diag.get('title')!r} "
                     f"gql_responses={diag.get('gql_responses')} "
                     f"gql_posts={diag.get('gql_posts')} "
                     f"json_stories={diag.get('json_stories')} "
                     f"json_posts={diag.get('json_posts')} "
                     f"articles={diag.get('articles')} feed={diag.get('feed')} "
                     f"permalinks={diag.get('permalinks')} "
                     f"roles={json.dumps(diag.get('roles'))}")
            if len(posts) == 0:
                self.log("[fb] all_links=" +
                         json.dumps(diag.get("all_links"))[:1600])
                self.log("[fb] containers=" +
                         json.dumps(diag.get("containers"))[:1800])
        return posts


    async def fetch_favorites(self, max_scroll: int = 6) -> list:
        """
        Read the account's FAVORITES feed — a single real news feed of every
        page the account has favorited. Because it is a genuine feed (not a page
        profile), it infinite-scrolls and fires the graphql calls we capture, so
        this is the path that yields the rich data. Each post is attributed back
        to its own author page, so downstream can scope it to whichever project
        tracks that page. Returns records for ALL authors in the feed.
        """
        ok, used = _bandwidth_ok(self.meter_db, self.cap_bytes)
        if not ok:
            self.log(f"[fb] monthly bandwidth cap reached ({used/1e9:.1f} GB) — "
                     f"skipping favorites")
            return []

        self._bytes = 0
        self._gql = []
        self._gql_bytes = 0
        # Facebook only builds the Favorites feed through in-app navigation — a
        # direct URL load renders an empty page. So open home, then CLICK the
        # Favourites entry in the Feeds sidebar, exactly like a person does.
        home = os.getenv("FB_HOME_URL", "https://www.facebook.com/")
        diag = {}
        posts = []
        source = "none"
        try:
            await self._page.goto(home, wait_until="domcontentloaded", timeout=60000)
            await self._page.wait_for_timeout(3000)
            if self._is_login_wall(self._page.url):
                self.log("[fb] favorites: session logged out — attempting re-login")
                if await self._login():
                    await self._page.goto(home, wait_until="domcontentloaded", timeout=60000)
                    await self._page.wait_for_timeout(3000)
                if self._is_login_wall(self._page.url):
                    self.log(f"[fb] favorites: NOT LOGGED IN. url={self._page.url}")
                    _record_bytes(self.meter_db, self._bytes)
                    return []
            # Reach the actual Favorites feed by in-app navigation (a cold URL
            # load renders an empty page). We only trust the result if the URL
            # ends up carrying filter=favorites — otherwise we're on the home
            # feed and must NOT treat everyone-you-follow as a favorite.
            self.on_favorites = await self._open_favorites()
            self.log(f"[fb] favorites: reached favorites feed = {self.on_favorites} "
                     f"(url={self._page.url})")
            await self._page.wait_for_timeout(random.randint(2000, 4000))
            try:
                await self._page.wait_for_selector('[role="article"]', timeout=20000)
            except Exception:
                pass
            await self._page.wait_for_timeout(random.randint(1500, 3000))
            await self._human_scroll(max_scroll)
            res = await self._page.evaluate(_EXTRACT_JS)
            if isinstance(res, dict):
                diag = res.get("diag") or {}
            gql = _stories_from_graphql(self._gql)
            diag["gql_responses"] = len(self._gql)
            diag["gql_posts"] = len(gql)
            if gql:
                posts, source = self._build_feed(gql), "gql"
            elif res.get("json_posts"):
                posts, source = self._build_feed(res["json_posts"]), "json"
            else:
                posts, source = self._build_feed(res.get("dom_posts") or []), "dom"
        except Exception as e:
            self.log(f"[fb] favorites failed: {type(e).__name__}: {e}")

        posts = await self._cache_media(posts)
        _record_bytes(self.meter_db, self._bytes)
        await self._save_state()
        authors = sorted({p["page"] for p in posts})
        self.log(f"[fb] favorites: {len(posts)} posts from {len(authors)} pages "
                 f"via {source}, {self._bytes//1024} KB")
        try:
            diag["handle"] = "favorites"
            diag["parsed"] = len(posts)
            diag["authors"] = authors[:40]
            with open(os.getenv("FB_DIAG_PATH", "fb_diag.json"), "w") as f:
                json.dump(diag, f)
        except Exception:
            pass
        self.log(f"[fb] favorites: diag — title={diag.get('title')!r} "
                 f"gql_responses={diag.get('gql_responses')} "
                 f"gql_posts={diag.get('gql_posts')} "
                 f"articles={diag.get('articles')} authors={authors[:20]}")
        # Whether we are actually logged in tells us if this is a session
        # problem (the usual cause) vs. a page-structure problem.
        head = (diag.get("body_head") or "").lower()
        logged_out = any(w in head for w in
                         ("log in", "log into", "create new account",
                          "forgot password", "sign up"))
        self.log(f"[fb] favorites: logged_in={not logged_out} "
                 f"body_head={diag.get('body_head')!r}")
        return posts


def _fallback_id(permalink: str) -> str | None:
    if not permalink:
        return None
    m = re.search(r"pfbid[0-9A-Za-z]+", permalink)
    if m:
        return m.group(0)
    m = re.search(r"(\d{6,})", permalink)
    return m.group(1) if m else None
