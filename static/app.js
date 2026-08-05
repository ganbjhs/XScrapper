/* Dashboard behaviour. Assumes helpers.js has loaded. */
const PAGE_SIZE = 100;
let offset = 0, loaded = 0, lastTotal = 0;

/* Built in ONE place so search, the auto-update tick and Download can never
   disagree about what is being shown. A filter added here is applied by all
   three; a filter added at a call site would silently apply to one of them. */
function params(){
  const p = new URLSearchParams();
  const add = (k,v) => { if(v) p.set(k,v); };
  add("q", $("#q").value.trim());
  add("author", $("#author").value.trim());
  add("since", $("#since").value);
  add("min_likes", $("#minlikes").value);
  add("min_views", $("#f_min_views").value);
  add("min_followers", $("#f_min_followers").value);
  add("from_date", $("#f_from").value);
  add("to_date", $("#f_to").value);
  add("category", $("#f_category").value);
  add("lang", $("#lang").value.trim());
  add("order", $("#order").value);
  add("stream", activeStream);
  if ($("#f_verified").checked) p.set("verified","1");
  if ($("#media").checked) p.set("has_media","1");
  if ($("#noretweets").checked) p.set("no_retweets","1");
  return p;
}

/* What is narrowing the results, in words, always on screen. A filter you
   forgot you set reads as "the collector stopped finding things" — which is
   the single most expensive misreading this dashboard can produce. */
const FILTER_LABELS = {
  author: "from", since: "within", min_likes: "min likes", min_views: "min views",
  min_followers: "min followers", from_date: "from", to_date: "to",
  category: "category", lang: "language", verified: "verified only",
  has_media: "has media", no_retweets: "no retweets",
};

function durationLabel(){
  const el = $("#since");
  return el && el.value ? el.options[el.selectedIndex].text.toLowerCase() : "";
}

function describeFilters(){
  const p = params();
  const bits = [];
  for (const [k, name] of Object.entries(FILTER_LABELS)) {
    const v = p.get(k);
    if (!v) continue;
    if (k === "since") { bits.push(durationLabel()); continue; }
    bits.push(v === "1" ? name : `${name} ${v}`);
  }
  return bits;
}

function refreshFilterChrome(){
  const bits = describeFilters();
  const n = $("#filtcount"), a = $("#activefilters"), s = $("#f_summary");
  if (n){ n.textContent = bits.length; n.hidden = bits.length === 0; }
  if (a) a.textContent = bits.length ? "· filtered: " + bits.join(", ") : "";
  if (s) s.textContent = bits.length
    ? `${bits.length} filter${bits.length>1?"s":""} — applies to what you see and to Download`
    : "No filters. Everything collected is shown.";
}

async function search(append){
  if (!append){ offset = 0; loaded = 0; }
  const p = params();
  p.set("limit", PAGE_SIZE);
  p.set("offset", offset);

  let d;
  try { d = await api("/api/tweets?" + p); }
  catch (e) { return banner(esc(e.message).replace(/\n/g,"<br>"), "err"); }
  banner("");

  lastTotal = d.total || 0;
  loaded += (d.rows || []).length;
  $("#count").textContent = lastTotal
    ? `showing ${loaded} of ${lastTotal}` : "nothing found";

  if (!d.rows || !d.rows.length){
    if (!append) {
      const win = durationLabel();
      $("#results").innerHTML = `<div class="empty">`
        + (win ? `Nothing posted in the ${esc(win)}.`
               : `No saved tweets match that.`)
        + `<br><span class="muted">`
        + (win ? `Widen <b>Duration</b> above to see older tweets. `
               : ``)
        + ($("#q").value.trim()
            ? 'Or press <b>Get new tweets</b> to ask X for them.' : '')
        + `</span></div>`;
    }
    return;
  }
  const html = d.rows.map(card).join("");
  if (append) $("#more")?.remove(), $("#results").insertAdjacentHTML("beforeend", html);
  else {
    // A fresh search resets the high-water mark, otherwise changing filters
    // would suppress everything older than whatever the previous filter showed.
    $("#results").innerHTML = html;
    topId = null; pending = []; $("#newbar").style.display = "none";
  }
  noteTop(d.rows);

  if (loaded < lastTotal){
    $("#results").insertAdjacentHTML("beforeend",
      `<button id="more" style="width:100%;padding:10px">Show ${
        Math.min(PAGE_SIZE, lastTotal - loaded)} more (${lastTotal - loaded} left)</button>`);
    $("#more").onclick = () => { offset += PAGE_SIZE; search(true); };
  }
}

/* Media rendering.
   mp4s from video.twimg.com play inline — the URL in media_urls is the direct
   file (verified: 200 video/mp4, byte-range capable), so a plain <video> works.
   X BROADCASTS CANNOT BE PREVIEWED: x.com sends
   `frame-ancestors 'self' https://x.com`, so any iframe from this page is
   refused by the browser. They get an honest labelled link instead of an
   embed that would render as a blank box. */
const YT = /(?:youtube\.com\/watch\?v=|youtu\.be\/)([A-Za-z0-9_-]{11})/;

function mediaHtml(t){
  const bits = [];

  /* Videos show their THUMBNAIL until you click.

     Every card used to mount a real <video> pointing at video.twimg.com.
     Even with preload="metadata" that is a request per clip, so scrolling a
     page of 100 tweets pulled dozens of videos nobody watched — and X's media
     URLs are signed and expire, so most of them came back 403 and rendered as
     black boxes. A still is a few tens of KB against several MB, and it cannot
     expire into a broken player.

     media[] carries {type,url,thumb}. Older rows have only the flat
     media_urls, so fall back to the previous behaviour for those rather than
     showing them nothing. */
  const media = (t.media && t.media.length)
    ? t.media
    : (t.media_urls || []).map(u => ({
        type: /\.(jpg|jpeg|png|webp)(\?|$)/i.test(u) ? "photo"
            : /\.mp4(\?|$)/i.test(u) ? "video" : "other",
        url: u, thumb: null }));

  for (const m of media){
    if (m.type === "photo"){
      bits.push(`<img src="${esc(m.thumb || m.url)}" loading="lazy" alt="">`);
    } else if (m.type === "video" || m.type === "gif"){
      const dur = m.duration ? `<span class="dur">${clock(m.duration)}</span>` : "";
      bits.push(m.thumb
        ? `<button class="playwrap" data-play="${esc(m.url)}" data-kind="${esc(m.type)}"
                   title="Play">
             <img src="${esc(m.thumb)}" loading="lazy" alt="">
             <span class="playbtn">▶</span>${dur}
           </button>`
        : `<video src="${esc(m.url)}" controls preload="none"
                  playsinline muted loop></video>`);
    } else if (/\.m3u8(\?|$)/i.test(m.url)){
      bits.push(`<a class="medialink" href="${esc(m.url)}" target="_blank" rel="noopener">
                   <b>Video stream</b><span>cannot play here — opens in a new tab</span></a>`);
    }
  }

  for (const u of (t.urls || [])){
    const yt = u.match(YT);
    if (yt){
      bits.push(`<iframe class="yt" src="https://www.youtube-nocookie.com/embed/${esc(yt[1])}"
                  loading="lazy" allowfullscreen
                  referrerpolicy="strict-origin-when-cross-origin"></iframe>`);
    } else if (/x\.com\/i\/broadcasts\//.test(u) || /pscp\.tv/.test(u)){
      bits.push(`<a class="medialink live" href="${esc(u)}" target="_blank" rel="noopener">
                   <b>● Live broadcast</b>
                   <span>X does not allow these to play here — opens on x.com</span></a>`);
    }
  }
  return bits.length ? `<div class="media">${bits.join("")}</div>` : "";
}

function card(t){
  const media = mediaHtml(t);
  return `<article class="card">
    <div class="top">
      <span class="name">${esc(t.author_display_name || t.author_username)}</span>
      <span class="handle">@${esc(t.author_username)}</span>
      <span class="when">· ${ago(t.created_at)}</span>
      ${t.lang ? `<span class="handle">· ${esc(t.lang)}</span>` : ""}
    </div>
    <div class="text">${esc(t.text)}</div>
    ${media}
    <div class="metrics">
      <span>♥ ${num(t.like_count)}</span>
      <span>⟳ ${num(t.retweet_count)}</span>
      <span>↩ ${num(t.reply_count)}</span>
      ${t.view_count ? `<span>👁 ${num(t.view_count)}</span>` : ""}
      ${t.lag_ms != null ? `<span title="how long after it was posted we saved it">⏱ ${(t.lag_ms/1000).toFixed(1)}s</span>` : ""}
      <a href="${esc(t.url)}" target="_blank" rel="noopener">see on X ↗</a>
    </div>
  </article>`;
}

async function status(){
  let d;
  try { d = await api("/api/status"); }
  catch (e) {
    $("#accounts").innerHTML = '<span class="muted">server unreachable</span>';
    return banner(esc(e.message).replace(/\n/g,"<br>"), "err");
  }

  const all = d.streams || [];

  $("#streams").innerHTML = all.length
    ? `<button class="streambtn ${activeStream?'':'active'}" data-s="">Everything</button>` +
      all.map(s => `<div class="streamrow">
        <button class="streambtn ${activeStream===s.label?'active':''} ${s.paused?'off':''}"
          data-s="${esc(s.label)}" title="${esc(s.query || s.label)}">
          ${esc(s.label)} <span class="muted">· ${s.count} tweets</span>
          ${s.paused ? '<span class="muted">· paused</span>' : ''}
          ${s.tg_enabled ? '<span class="tgon">· → Telegram</span>' : ''}
          ${!s.watched && s.tg_enabled
              ? '<span class="warnbit">· starting — restart the watcher if this stays</span>'
              : (!s.watched
                  ? '<span class="warnbit">· one-off search, not being watched</span>' : '')}
          ${s.lag_p50!=null ? `<span class="muted">· usually saved ${secs(s.lag_p50)} after posting</span>`:''}
        </button>
        <button class="streamx" data-gear="${esc(s.label)}" title="Settings">⚙</button>
      </div>
      <div class="streamcfg" data-cfg="${esc(s.label)}" hidden></div>`).join("")
    : '<span class="muted">nothing yet</span>';

  document.querySelectorAll(".streambtn").forEach(b =>
    b.onclick = () => { activeStream = b.dataset.s; status(); search(); });

  // Bind in the same pass that draws them: status() replaces this whole panel
  // every 15s, so anything bound earlier belongs to nodes that no longer exist.
  document.querySelectorAll("[data-gear]").forEach(b => b.onclick = () => {
    const label = b.dataset.gear;
    // Closing a panel with unsent edits is how a configuration silently fails
    // to exist. Ask rather than discard.
    if (cfgDirty && openCfg === label &&
        !confirm("You have unsaved settings.\n\nClose without sending them?")) return;
    cfgDirty = false;
    openCfg = (openCfg === label) ? null : label;
    drawCfg(all);
  });
  drawCfg(all);

  /* Only what you glance at.
     The reasons, remedies and per-account facts moved to /accounts — in a
     300px column every unhealthy account produced a paragraph of small grey
     text, and none of it is something you act on mid-search. What matters here
     is "is anything collecting", and if not, that something is wrong. */
  /* "Collecting" is `active`, NOT status === "live".
     Amber is not a weaker red (R12): an account with no proxy and no
     known-device cookie is flagged amber and is collecting perfectly well.
     Counting only green made this box announce "Nothing is collecting right
     now" while the one account was, in fact, collecting. */
  const accts  = d.accounts || [];
  const good   = accts.filter(a => a.active);
  const bad    = accts.length - good.length;

  $("#accounts").innerHTML = accts.length
    ? good.map(a => `<div class="row">
          <span class="k">@${esc(a.username)}${a.proxy?' <span title="uses a proxy">⛓</span>':''}</span>
          <span class="flag ${a.status === "warning" ? "warning" : "live"}">${
            a.status === "warning" ? "Working*" : "Working"}</span>
        </div>`).join("")
      // Never silently omit the unhealthy ones: an empty panel and a panel
      // hiding three dead accounts must not look the same (R12).
      + (bad ? `<div class="row"><span class="k">${bad} not collecting</span>
                  <span class="flag ${good.length ? "warning" : "dead"}">see below</span></div>` : "")
      + (good.length ? "" : '<div class="muted">Nothing is collecting right now.</div>')
    : '<span class="muted">none yet</span>';

  // Both queues, named. They are separate allowances that do not share, so
  // showing one number invites spending the wrong budget.
  const QNAME = {search: "word searches", list: "lists"};
  const budget = Object.entries(d.budget || {}).map(([q, b]) =>
    `<div class="row"><span class="k">${QNAME[q] || q}</span>
       <span>${b.remaining} of ${b.limit}</span></div>` +
    (b.resets_in != null
      ? `<div class="muted" style="margin:-2px 0 4px 2px;font-size:11px">
           ${b.rolled ? "window reset — full again"
                      : `resets in ${secs(b.resets_in)}`}</div>` : "")
  ).join("");

  $("#totals").innerHTML =
    `<div class="row"><span class="k">tweets</span><span>${d.totals.tweets ?? 0}</span></div>` +
    (budget ? `<div class="cfghead" style="margin-top:6px">Requests left</div>` + budget : "") +
    (d.totals.note ? `<div class="muted">${esc(d.totals.note)}</div>` : "");

  const kset = (id, v) => { const e = $("#" + id); if (e) e.textContent = v; };
  kset("kpi-saved", (d.totals.tweets ?? 0).toLocaleString());
  kset("kpi-watch", all.length + (all.length === 1 ? " source" : " sources"));
  kset("kpi-acct", (d.accounts || []).filter(a => a.active).length + " active");
}

/* ------------------------------------------------------------------
   Per-stream settings, behind the gear.

   Kept collapsed and rendered on demand: the sidebar redraws every 15s, and
   an always-open form would fight whatever you were typing into it. Only the
   one you opened is built, and it survives the redraw because openCfg is
   module state rather than DOM state.
   ------------------------------------------------------------------ */
const SPEED_LABELS = {"":"leave as configured", fastest:"as fast as allowed (~5s)",
  fast:"every 15s or so", normal:"every minute or so", slow:"every 5 minutes or so",
  quarter:"every 15 minutes or so", hourly:"every half hour or so"};

function drawCfg(streams){
  document.querySelectorAll("[data-cfg]").forEach(box => {
    const label = box.dataset.cfg;
    if (label !== openCfg){ box.hidden = true; box.innerHTML = ""; return; }

    // NEVER rebuild a panel that has unsent edits in it. status() redraws this
    // sidebar every 15s and after every save, and a rebuild replaces innerHTML
    // — so a chat id typed but not yet sent was being destroyed on a timer,
    // with no error and no sign it had happened. Leaving the DOM alone while
    // it is dirty is what makes "type it, then press Save" reliable.
    if (cfgDirty && box.innerHTML){ box.hidden = false; return; }

    const s = streams.find(x => x.label === label) || {};
    box.hidden = false;
    box.innerHTML = `
      <label class="cfgrow">How often to check
        <select data-k="speed">${Object.entries(SPEED_LABELS).map(([v,t]) =>
          `<option value="${v}" ${s.speed===v?"selected":""}>${t}</option>`).join("")}</select>
      </label>
      <label class="cfgrow">Tweets per check
        <select data-k="pages">
          <option value="">leave as configured</option>
          <option value="1"  ${s.pages===1?"selected":""}>about 20</option>
          <option value="5"  ${s.pages===5?"selected":""}>about 100</option>
          <option value="10" ${s.pages===10?"selected":""}>about 200</option>
        </select>
      </label>
      <label class="cfgchk"><input type="checkbox" data-k="paused" ${s.paused?"checked":""}>
        Pause — stop checking this for now</label>

      <div class="cfghead">Send to Telegram</div>
      <label class="cfgchk"><input type="checkbox" data-k="tg_enabled" ${s.tg_enabled?"checked":""}>
        Watch this continuously and send every new tweet to Telegram</label>
      <div class="cfgnote">Switching this on also makes the collector keep
        checking ${esc(s.query || s.label)} on its own — it is not just a
        forwarding switch. Only tweets found <b>after</b> you save are sent;
        what is already collected is not resent.</div>
      <label class="cfgrow">Send where
        <input data-k="tg_chat_id" placeholder="e.g. -1003964750953 or @mychannel"
               value="${esc(s.tg_chat_id||"")}"></label>
      <label class="cfgrow">Only if it has at least this many likes
        <input data-k="tg_min_likes" type="number" min="0"
               value="${s.tg_min_likes||0}"></label>
      <label class="cfgchk"><input type="checkbox" data-k="tg_skip_retweets"
        ${s.tg_skip_retweets?"checked":""}> Skip retweets</label>
      <label class="cfgchk"><input type="checkbox" data-k="tg_skip_replies"
        ${s.tg_skip_replies?"checked":""}> Skip replies</label>
      <label class="cfgrow">Only if POSTED within this many hours (0 = no limit)
        <input data-k="tg_max_age_h" type="number" min="0"
               value="${s.tg_max_age_h||0}"></label>
      <div class="cfgnote">Delivery normally keys on when a tweet was
        <b>collected</b>, so a stream that has just started sends its whole
        backlog — posts weeks old arriving as if new. This bounds it by when the
        tweet was actually <b>published</b>.</div>

      <div class="cfgsave">
        <button class="cfgbtn primary" data-save>Save settings</button>
        <button class="cfgbtn" data-tgtest>Send 3 test tweets</button>
        <span class="cfgdirty" data-dirty hidden>not sent yet</span>
      </div>

      <div class="cfghead">Remove</div>
      <button class="cfgbtn" data-act="stop">Stop watching — keep the tweets</button>
      <button class="cfgbtn danger" data-act="wipe">Delete this and its tweets</button>
      <div class="muted" style="margin-top:4px;font-size:11px">
        Stopping is reversible. Deleting is not — X only lets you look back
        about 7 days.</div>`;

    // ONE EXPLICIT SEND, carrying every field.
    //
    // This replaced a per-control autosave bound to `onchange`. That looked
    // tidier and was quietly broken for the fields that mattered most: on a
    // text input `onchange` fires only on BLUR, so typing a Telegram chat id
    // and then clicking the enable checkbox sent the checkbox and never the
    // chat id. The stream ended up enabled with nowhere to send, which the
    // backend logs as "enabled but no chat id — skipping" and the operator
    // sees as silence. Marking dirty on `input` and sending the whole form on
    // one press removes the entire class of bug.
    const markDirty = () => {
      cfgDirty = true;
      const tag = box.querySelector("[data-dirty]");
      if (tag) tag.hidden = false;
    };
    box.querySelectorAll("[data-k]").forEach(el => {
      el.oninput = markDirty;
      el.onchange = markDirty;
    });

    box.querySelector("[data-save]").onclick = async (ev) => {
      const btn = ev.currentTarget;
      const body = {label};
      box.querySelectorAll("[data-k]").forEach(el => {
        body[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
      });
      btn.disabled = true;
      try {
        const r = await api("/api/stream/settings", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify(body)
        });
        if (r.error) return banner(esc(r.error), "err");
        // Only now is it safe to let the redraw rebuild this panel.
        cfgDirty = false;
        const tag = box.querySelector("[data-dirty]");
        if (tag) tag.hidden = true;
        banner(body.tg_enabled
          ? `Saved. Sending <b>${esc(label)}</b> to Telegram ${esc(body.tg_chat_id || "(default chat)")}.`
          : `Saved settings for <b>${esc(label)}</b>.`, "ok");
        await status();
      } catch (e) { banner(esc(e.message), "err"); }
      finally { btn.disabled = false; }
    };

    // Tests THIS stream with THIS panel's chat id, including one you have
    // typed but not yet saved — otherwise "test" would check the old value and
    // tell you the new one works.
    box.querySelector("[data-tgtest]").onclick = async (ev) => {
      const btn = ev.currentTarget;
      const chat = (box.querySelector('[data-k="tg_chat_id"]').value || "").trim();
      btn.disabled = true;
      banner("Sending 3 real tweets to Telegram…");
      try {
        const d = await api("/api/settings/telegram/test", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({chat_id: chat, stream: label})
        });
        banner(d.error ? "Telegram said: " + esc(d.error)
             : (d.real ? `Sent ${d.sent} real tweet(s) from <b>${esc(label)}</b>, one per message.`
                       : "Bot and chat id work, but this stream has no tweets yet."),
               d.error ? "err" : "ok");
      } catch (e) { banner(esc(e.message), "err"); }
      finally { btn.disabled = false; }
    };

    box.querySelectorAll("[data-act]").forEach(b => b.onclick = () =>
      removeStream(label, b.dataset.act === "wipe", s.count || 0));
  });
}

async function removeStream(label, wipe, count){
  let body = {label};
  if (wipe){
    const typed = prompt(
      `Delete "${label}" AND its ${count} tweets?\n\n` +
      `This cannot be undone. X only lets you look back about 7 days, so ` +
      `anything older than that can never be collected again.\n\n` +
      `Tweets also matched by another list are kept.\n\n` +
      `Type the name to confirm:`);
    if (typed === null) return;
    body = {label, delete_tweets: true, confirm: typed.trim()};
  } else if (!confirm(`Stop watching "${label}"?\n\n` +
                      `Its ${count} tweets stay and are still searchable.`)) {
    return;
  }
  let d;
  try {
    d = await api("/api/stream/remove", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
  } catch (e) { return banner(esc(e.message), "err"); }
  if (d.error) return banner(esc(d.error), "err");

  if (activeStream === label) activeStream = "";
  openCfg = null;
  banner(wipe
    ? `Deleted <b>${esc(label)}</b> and ${d.tweets_deleted} tweet(s).`
    : `Stopped watching <b>${esc(label)}</b>. Its tweets are still here.`, "ok");
  await status(); await search();
}

$("#src").onchange = () => {
  const isList = $("#src").value === "list";
  $("#q").placeholder = isList
    ? "Paste an X list link — https://x.com/i/lists/1234567890"
    : "Type words to find in the tweets you have saved…";
  /* Lists are a far better deal than searches on BOTH counts, and the numbers
     are not close.
       requests : 500 per 15 min against 50
       per request : X returns exactly the 20 asked for on a search, and
                     ignores the count on a list — measured 78 to 92, so ~90.
     Quoting 20 per request for a list understated it by four and a half times:
     "5 pages, about 100 tweets" actually fetched 460. */
  const per = isList ? 90 : 20;
  [...$("#pages").options].forEach(o => {
    const n = parseInt(o.value, 10);
    o.textContent = `about ${n*per} tweets · uses ${n} of your `
      + (isList ? "500" : "50") + " requests";
  });
};
$("#src").onchange();

$("#go").onclick = search;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });

// Sorting re-runs on its own; the filters wait for Apply. Re-querying on every
// keystroke of "min followers" would fire a query per digit and make 10000
// pass through 1, 10, 100 and 1000 on the way.
$("#order").addEventListener("change", search);
$("#since").addEventListener("change", () => { refreshFilterChrome(); search(); });

const FILTER_IDS = ["author","minlikes","lang","media","noretweets",
                    "f_min_views","f_min_followers","f_from","f_to",
                    "f_category","f_verified"];
FILTER_IDS.forEach(id => {
  const el = $("#"+id);
  if (el){ el.addEventListener("input", refreshFilterChrome);
           el.addEventListener("change", refreshFilterChrome); }
});

$("#filtbtn").onclick = () => {
  const box = $("#filters");
  box.hidden = !box.hidden;
  if (!box.hidden) refreshFilterChrome();
};
$("#f_apply").onclick = () => { refreshFilterChrome(); search(); };
$("#f_clear").onclick = () => {
  FILTER_IDS.forEach(id => {
    const el = $("#"+id);
    if (!el) return;
    if (el.type === "checkbox") el.checked = false; else el.value = "";
  });
  refreshFilterChrome();
  search();
};
refreshFilterChrome();

$("#csv").onclick = () => { location = "/api/export?" + params(); };

$("#getnew").onclick = async () => {
  const raw = $("#q").value.trim();
  const isList = $("#src").value === "list";
  if (!raw) return banner(isList
      ? "Paste an X list link first."
      : "Type what to look for first.", "err");
  const query = isList ? "" : raw, listId = isList ? raw : "";
  const pages = parseInt($("#pages").value, 10);

  /* Ask the guard BEFORE spending anything. This is the whole point: the
     dangerous click is the one you make without knowing the cost. */
  let g;
  try { g = await api(`/api/guard?action=fetch&cost=${pages}&queue=${isList?"list":"search"}`); }
  catch (e) { return banner(esc(e.message).replace(/\n/g,"<br>"), "err"); }

  const blocks = g.findings.filter(f => f.level === "block");
  const warns  = g.findings.filter(f => f.level === "warn");

  if (blocks.length){
    return banner(
      `<b>Cannot do that right now — ${esc(blocks[0].title)}</b><br>${esc(blocks[0].detail)}` +
      (blocks[0].remedy ? `<br><b>What to do:</b> ${esc(blocks[0].remedy)}` : ""), "err");
  }

  let msg = `Ask X for about ${pages*20} tweets matching:\n\n${raw}\n\n` +
            `This uses ${pages} of your ${isList ? 500 : 50} requests ` +
            `for the next 15 minutes.\n`;
  if (warns.length){
    msg += "\nWorth knowing first:\n  • "
         + warns.map(w => w.title + (w.remedy ? `\n    ${w.remedy}` : "")).join("\n  • ") + "\n";
  }
  msg += "\nAnything found is saved. Go ahead?";
  if (!confirm(msg)) return;

  $("#getnew").disabled = true;
  banner("Asking X…");
  try {
    const d = await api("/api/fetch", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({query, list_id:listId, tab:"Latest", pages, ack:true})
    });

    if (d.error) banner("X said: " + esc(d.error), "err");
    else if (d.hint) banner(esc(d.hint), "err");
    else if (d.stop === "no_account_or_abort")
      banner("No X account was free to do this. Check the accounts panel, or " +
             "wait a few minutes for the request allowance to reset.", "err");
    else {
      let msg = `Found ${d.results} tweets — ${d.new} new, ${d.dup} you already had. ` +
                `${d.rl_remaining} of ${d.rl_limit} requests left for the next 15 minutes.`;
      if (d.stop === "exhausted" && d.pages < pages)
        msg += ` That is everything X has for this.`;
      banner(msg, "ok");
      activeStream = d.stream;
      $("#q").value = "";        // the search now lives as a filter on the left
    }
    await status(); await search();
  } catch (e) {
    banner(esc(e.message).replace(/\n/g,"<br>"), "err");
  } finally { $("#getnew").disabled = false; }
};

/* Click a thumbnail to fetch and play that one video.

   Delegated from #results rather than bound per card: the list is re-rendered
   on every search, every auto-update tick and every "show more", so handlers
   attached to individual cards would belong to nodes that no longer exist. */
$("#results").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-play]");
  if (!btn) return;
  const gif = btn.dataset.kind === "gif";
  const v = document.createElement("video");
  v.src = btn.dataset.play;
  v.controls = !gif; v.autoplay = true; v.playsInline = true;
  v.muted = gif; v.loop = gif;
  // If the URL has expired — X signs them and they do expire — say so instead
  // of leaving a black rectangle where the video was.
  v.onerror = () => {
    const a = document.createElement("a");
    a.className = "medialink"; a.target = "_blank"; a.rel = "noopener";
    a.href = btn.dataset.play;
    a.innerHTML = "<b>Video unavailable</b><span>X's link has expired — opens on x.com</span>";
    v.replaceWith(a);
  };
  btn.replaceWith(v);
});

/* Standing risk panel. The costly mistakes here are the silent ones, so the
   state that makes an action dangerous is always on screen — not only at the
   moment you click. */
async function risks(){
  let g;
  try { g = await api("/api/guard"); } catch { return; }
  const items = g.findings.filter(f => f.level !== "info");
  const box = $("#riskbox");
  if (!items.length){ box.hidden = true; return; }
  box.hidden = false;
  $("#risks").innerHTML = items.map(f => `
    <div style="margin-bottom:9px">
      <span class="pill ${f.level==='block'?'bad':''}"
            style="${f.level==='warn'?'background:rgba(224,168,0,.18);color:#b8860b':''}">
        ${f.level==='block'?'Stops work':'Worth fixing'}</span>
      <div style="margin-top:3px">${esc(f.title)}</div>
      ${f.remedy?`<div class="muted" style="margin-top:2px">→ ${esc(f.remedy)}</div>`:''}
    </div>`).join("");
}

$("#tgtoggle").onclick = () => {
  const box = $("#tgbox");
  box.hidden = !box.hidden;
  if (!box.hidden) $("#tg_token").focus();
};
$("#tg_save").onclick = async () => {
  $("#tg_save").disabled = true;
  try {
    const d = await api("/api/settings/telegram", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({token: $("#tg_token").value.trim(),
                            chat_id: $("#tg_chat").value.trim()})
    });
    if (d.error) return banner(esc(d.error), "err");
    // Never echo the token back into the page once it is stored.
    $("#tg_token").value = "";
    $("#tg_token").placeholder = d.has_token ? "saved — paste again to replace"
                                             : "bot token from @BotFather";
    banner("Telegram saved. Switch it on for a list with the ⚙ beside it.", "ok");
  } catch (e) { banner(esc(e.message), "err"); }
  finally { $("#tg_save").disabled = false; }
};
$("#tg_test").onclick = async () => {
  $("#tg_test").disabled = true;
  banner("Sending a test message…");
  try {
    const d = await api("/api/settings/telegram/test", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({chat_id: $("#tg_chat").value.trim()})
    });
    banner(d.error ? "Telegram said: " + esc(d.error)
                   : "Sent. Check Telegram — if nothing arrived, the chat id is wrong.",
           d.error ? "err" : "ok");
  } catch (e) { banner(esc(e.message), "err"); }
  finally { $("#tg_test").disabled = false; }
};

/* ------------------------------------------------------------------
   Auto-update.

   This re-reads the LOCAL DATABASE on a timer. It never calls X — the
   watcher service already polls X continuously on its own adaptive
   interval, and new tweets land in results.db as it goes. Auto-fetching
   from the browser every 15s would be 240 requests/hour against a ceiling
   of ~200, so it would rate-limit the watcher out of existence within the
   hour. Reading the database is free and shows the same tweets.

   New rows are PREPENDED rather than replacing the list, so scroll
   position, "Show more" pages and reading position all survive a refresh.

   The checkbox is #autorefresh, NOT #live. It used to share the id "live"
   with the fetch button, so $("#live") returned the button, .checked was
   undefined, and tick() bailed on its first line every single time — the
   dot pulsed and nothing ever refreshed.
   ------------------------------------------------------------------ */
let liveTimer = null, pending = [], topId = null;

const bigger = (a, b) => {           // tweet ids exceed 2^53; compare as BigInt
  try { return BigInt(a) > BigInt(b); } catch { return false; }
};

function noteTop(rows){
  for (const r of rows) if (!topId || bigger(r.tweet_id, topId)) topId = r.tweet_id;
}

function showPending(){
  if (!pending.length) return;
  const html = pending.map(t => card(t).replace('class="card"', 'class="card new"')).join("");
  $("#results").insertAdjacentHTML("afterbegin", html);
  noteTop(pending);
  loaded += pending.length; lastTotal += pending.length; offset += pending.length;
  $("#count").textContent = `showing ${loaded} of ${lastTotal}`;
  pending = [];
  $("#newbar").style.display = "none";
}

async function tick(){
  if (!$("#autorefresh").checked || document.hidden) return;
  let d;
  try {
    const p = params(); p.set("limit", PAGE_SIZE); p.set("offset", 0);
    d = await api("/api/tweets?" + p);
  } catch { return; }            // a blip should not spam the banner

  const fresh = (d.rows || []).filter(r => !topId || bigger(r.tweet_id, topId));
  if (!fresh.length) return;

  // At the top of the page, drop them straight in; otherwise offer a button so
  // the page never jumps under someone who is reading.
  pending = fresh.concat(pending);
  if (window.scrollY < 80) {
    showPending();
  } else {
    $("#newbtn").textContent = `${pending.length} new tweet${pending.length>1?'s':''} — show them`;
    $("#newbar").style.display = "block";
  }
}

function setLive(on){
  $("#livedot").classList.toggle("on", on);
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = on ? setInterval(tick, parseInt($("#everysec").value, 10) * 1000) : null;
}
$("#autorefresh").onchange = () => setLive($("#autorefresh").checked);
$("#everysec").onchange    = () => setLive($("#autorefresh").checked);
$("#newbtn").onclick    = () => { showPending(); window.scrollTo({top: 0, behavior: "smooth"}); };
// A hidden tab should not keep querying; catch up the moment it is visible.
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

status(); search().then(() => setLive(true)); risks();
setInterval(() => { status(); risks(); }, 15000);
