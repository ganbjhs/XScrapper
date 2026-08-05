/* Shared by every page: DOM helpers, the API client, and the streamed
   sign-in window. Loaded before any page's own script. */
const $ = s => document.querySelector(s);
let activeStream = "";
let openCfg = null;      // which stream has its settings open
let cfgDirty = false;    // settings typed but not yet sent to the backend

const esc = s => (s||"").replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function ago(iso){
  if(!iso) return "";
  const s = (Date.now() - new Date(iso).getTime())/1000;
  if (s < 60) return Math.max(0,Math.round(s))+"s ago";
  if (s < 3600) return Math.round(s/60)+"m ago";
  if (s < 86400) return Math.round(s/3600)+"h ago";
  return Math.round(s/86400)+"d ago";
}
const num = n => n == null ? "0" : n >= 1000 ? (n/1000).toFixed(1)+"K" : ""+n;

// A duration in seconds, said the way a person would say it.
function secs(s){
  if (s < 90)    return Math.round(s) + " seconds";
  if (s < 5400)  return Math.round(s/60) + " minutes";
  if (s < 86400) return Math.round(s/3600) + " hours";
  return Math.round(s/86400) + " days";
}

// A video's length, as a player shows it. X reports duration in MILLISECONDS —
// feeding it to secs() above turns a 5-minute clip into "3 days".
function clock(ms){
  const t = Math.round(ms/1000), m = Math.floor(t/60), s = t % 60;
  if (m < 60) return `${m}:${String(s).padStart(2,"0")}`;
  return `${Math.floor(m/60)}:${String(m%60).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

/* Distinguishes "the server is not running" from "the server said no".
   A bare "Failed to fetch" is useless — it is what the browser says when the
   backend is simply gone, which is the single most likely cause. */
async function api(url, opts){
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    throw new Error("Cannot reach the collector. It may have stopped — " +
                    "reload the page in a moment.");
  }
  const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

function banner(msg, kind){
  $("#banner").innerHTML = msg ? `<div class="banner ${kind||''}">${msg}</div>` : "";
}


function loginOpen(label){
  const w = window.open("/signin?label=" + encodeURIComponent(label),
                        "xs_signin_" + label, "width=1180,height=980");
  // NOTE the explicit +. JavaScript does not join adjacent string literals the
  // way Python and C do — "a" "b" is a syntax error, not "ab". Writing it the
  // Python way took the whole dashboard script down with "missing ) after
  // argument list", which reads like a bracket problem and is not one.
  if (!w) return banner(
    "Your browser blocked the sign-in window. Allow pop-ups for this site, " +
    "or open <a href=\"/signin?label=" + encodeURIComponent(label) + "\">this link</a>.",
    "err");
  banner("Sign in to the site in the new tab, then come back here.", "ok");
  // The tab tells us when it is done, so this page refreshes without being
  // asked and the account flips to Working while you are looking at it.
  window.addEventListener("message", (e) => {
    if (e.origin === location.origin && e.data === "signed-in") status();
  }, {once: true});
}
