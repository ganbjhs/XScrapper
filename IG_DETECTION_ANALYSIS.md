# Instagram: why the sessions keep breaking (2026-09-12)

Read alongside `IDENTITY_MODEL.md`, `RULEBOOK.md` §IG, and the CHECKPOINT entries
for 2026-09-03 → 2026-09-06. Nothing here is a code change; it is the diagnosis
and the order to fix things in.

---

## The short answer

**You are looking at two different problems wearing one name, and the second one
is not the one you think.**

1. **`proxy_broken` is not Instagram.** It is the pipe. Your classifier is
   right, and the root cause is already written down in your own CHECKPOINT:
   Webshare's "session-pinned" residential exits are not pinned. You cannot buy
   "one IP forever" from a rotating residential product. That is a purchasing
   decision, not a code bug.

2. **"Automated behaviour" is not a missing fingerprint.** Your fingerprint work
   (`ig_identity.py`) is better than most of what exists publicly. What is
   missing is **continuity**: the identity is coherent at the moment of sign-in
   and then decays, because *nothing writes session state back between passes*.
   Every collection pass re-presents the session exactly as it was at login,
   forever. A real client's session state advances. Yours is frozen.

The single highest-value finding in this document is §3.2. The single
highest-value *action* is §5.1 (static IPs). They are different items, and you
should do both.

And there is a door you did not ask about — see §6.

---

## 1. What I read

The Instagram layer, in the order a request travels:

| file | role |
|---|---|
| `ig_identity.py` | mints one coherent phone per account (catalogue, market, UA, Client Hints) |
| `ig_session.py` | the ONLY place a `Client` is built; owns the device seed and the sidecar |
| `signin.py` | the three doors: `ig_password`, `ig_cookie`, `ig_browser_adopt` |
| `ig.py` `InteractiveLogin` | the streamed Chromium door — persistent profile, phone-shaped context |
| `auth.py` `_launch` | `launch_persistent_context`, proxy split, channel fallback ladder |
| `engine_ig.py` | `build_client`, `_browser_session`, `resolve_user`'s four doors |
| `collect_ig.py` | the pass; `_proxy_broken` folds per-handle cards into one account condition |
| `decider.py` | the rule table; exception → condition → action |
| `ig_human.py` | the rhythm layer: day plans, log-normal gaps, budgets, warm-up |
| `guard.py` | surfaces the risk |

The doctrine is sound and unusually well documented. Most of what follows is
about the gap between what the docs say the system does and what the code
actually does on the wire.

---

## 2. "Broken proxy", diagnosed

### 2.1 The classification is correct — keep it

`decider.py:356-367` maps `ClientConnectionError`, `SSLError`,
`SSLCertVerificationError`, `ProxyError`, `ConnectionError`, `ConnectTimeout`,
`NewConnectionError`, `MaxRetryError` → `proxy_broken`.

That is right, and the reasoning in `RULEBOOK` line ~1163 ("classify the pipe
before the answer") is exactly correct and worth defending:

> **A connect-level exception cannot be Instagram.** Every Instagram-side
> refusal — `challenge_required`, `checkpoint_required`, `login_required`,
> `feedback_required`, `PleaseWaitFewMinutes`, 429 — arrives as a **completed
> HTTP response** with a parsed body. instagrapi raises all of those from
> response parsing, never from a socket. If TLS never completed, Instagram never
> saw the request and cannot be the cause.

So every `proxy_broken` you have ever seen is one of exactly four things:

- the residential exit device went offline mid-session
- the CONNECT tunnel to Webshare's edge timed out or was reset
- a middlebox re-signed the TLS (your Sophos case — `RULEBOOK` ~1171)
- Webshare's own edge intercepted you: auth, concurrency cap, or quota

### 2.2 Why it "sometimes fixes itself"

Because the pool rotates. The next request draws a different exit node, and the
new node works. **That self-healing is not luck — it is the disease.** The same
mechanism that recovers you is the mechanism that moves your account's IP.

Your own CHECKPOINT (2026-09-06, "Still open"):

> Webshare's session-pinned exits are not steady: two sign-ins two minutes apart
> left through two different IPs. Instagram will see the account moving within
> one ISP (which real phones on CGNAT also do), but "one IP forever" needs a
> static residential / ISP proxy per account.

You found this, wrote it down, and left it open. It is the answer to symptom 1
and half the answer to symptom 2.

### 2.3 Sticky sessions will not save you

Webshare's documented username syntax is
`{user}-{country}-{geo}-{session}` — e.g. `user-in-32:pass@p.webshare.io:80`
pins a session, `-rotate` explicitly does not (and the two are mutually
exclusive). Worth confirming the exact username shape you have stored in
`enc_proxy`, because if any account is on `-rotate` it is rotating **per
request**, which is the worst available configuration for a logged-in session.

But even with correct sticky syntax: **a sticky session is a best-effort mapping
in the provider's orchestration layer, upper-bounded by whether the residential
peer is still online.** Webshare's docs state no sticky TTL at all, and their
proxy-config API documents automatic replacement of "invalid, low-confidence,
underperforming, or failed" proxies — i.e. the pool silently swaps members under
you. (Decodo, which documents this honestly, says outright: "the longer the
session you have, the more chances there are that the IP will rotate before your
specified time due to the residential device at the end going offline.")

### 2.4 The second-order cost

`proxy_broken` carries `stop_account`, backoff 30m → 4h, and pages the admin.
That is a sensible response to a genuinely broken proxy. Today it mostly means
**a healthy account is idled for 30 minutes because a stranger's phone in
Lucknow went to sleep.** With a static IP, that backoff starts meaning what it
says.

---

## 3. "Automated behaviour", diagnosed

### 3.1 Gap 1 — the IP moves (your instinct was right)

You asked whether you are "not using static IPs." Yes, and it matters.

Honest evidence grading: Meta publishes nothing about its session-risk model.
The strongest non-vendor source is instagrapi's own maintainer, who names
**"proxy switching after each request"** as an explicit anti-pattern in the
Best Practices doc, and recommends holding the same country / city / ASN /
device / saved session across runs. Everything else on this topic is proxy-
vendor marketing and should be read as such. The class of system is well
documented elsewhere though — Microsoft publishes its "unfamiliar sign-in
properties" detector, which scores exactly IP + ASN + location + device +
browser against a learned per-account baseline. That is the shape of the thing,
even if Meta's specifics are unpublished.

The distinction that matters: **moving within one ISP** (Airtel Mumbai → Airtel
Mumbai) is what real phones on CGNAT do all day. **Moving across ASNs or
countries** is not. Right now you cannot tell which of those is happening,
because you only record `meta.exit` at sign-in.

### 3.2 Gap 2 — session state is written once and never again ← **the new finding**

`ig_session.persist()` is called from exactly three places:

```
signin.py:686     (all three sign-in doors, via _ig_persist)
ig_login.py:121   (CLI password login)
ig_import.py:105  (CLI cookie import)
```

**It is never called after a collection pass.** `collect_ig` calls
`ig_session.load_client()` (lines 524, 847), which reads the sidecar, builds a
fresh `Client`, collects — and then the process discards everything that pass
learned. Next pass reads the same sidecar again.

Two things get thrown away every single pass:

**`ig_www_claim`.** Instagram returns a value in the `x-ig-set-www-claim`
response header; the client is expected to store it and echo it back as
`X-IG-WWW-Claim` on every subsequent request, starting from the literal string
`"0"` if it has never received one. instagrapi implements this and keeps it in
`settings`. Your sidecar restores whatever the claim was **at the moment of the
last sign-in** and re-presents it forever. A real client's claim advances
continuously. Yours is a fossil with a login timestamp on it.

Your `DEVICE_KEYS` comment (`ig_session.py:72-74`) says `ig_www_claim` is
"session or transport state and is free to change between logins." That is a
correct statement about **the seed** — the claim has no business in the device
file. It is not an argument for *discarding* it.

**`mid`.** Same shape, worse. `_carry_jar` (`signin.py:594`) puts the browser's
`mid` cookie into the requests jar — good, that was the right instinct. But
`cl.mid`, which is what fills the `X-MID` **header** the app sends, is learned
by instagrapi from `ig-set-x-mid` response headers, not from the cookie jar.
The two local sidecars both show `"mid": null`. So there is a live question:

> Is this account's app-side `X-MID` the same value as its web-side `mid`
> cookie? They are the same identifier on two surfaces. If they differ, the
> session is presenting two device identities at once.

One log line at sign-in answers it. I would expect them to differ today.

**The fix is small.** A `ig_session.touch(cl, username, root)` that rewrites
*only the session half* of the sidecar — `cookies`, `authorization_data`,
`ig_www_claim`, `mid`, `last_login` — and never calls `save_device()`. Call it
at the end of every pass, and on a clean exit. It is `persist()` minus the
device write, roughly 30 lines, and it is the difference between a session that
ages and a session that is re-enacted.

### 3.3 Gap 3 — one session, two different clients (structural)

This is the deep one, and it is not fixable with a config change.

- The **browser** signs in: real Chrome TLS ClientHello and HTTP/2 settings,
  real JS execution, whatever client telemetry Instagram's web bundle fires,
  and a persistent `mid`/`datr` living in `profiles/<account>/`.
- Then **everything else** happens from instagrapi: a `requests` session with a
  static, unmistakably-not-Chrome TLS fingerprint, no JavaScript, no client
  telemetry — presenting a `sessionid` minted on the web surface while claiming
  in its User-Agent to be Instagram Android 428.0.0.47.67 on a Samsung.

The mechanism is visible in instagrapi's source: `login_by_sessionid()` builds
`authorization_data` from the sessionid and reuses whatever uuids the client
already has. **It does not perform the device binding a real password login
does** — a password login submits `phone_id` / `device_id` / `guid` in the login
payload and parses a server-issued `Authorization` header back. The sessionid
path skips that handshake entirely. There is a reproduced issue
(`subzeroid/instagrapi#2377`, Feb 2026) where `login_by_sessionid()` returned
`challenge_required` with the challenge URL pointing at
`.../web/unsupported_version/` — i.e. the server flagged the client signature as
inconsistent with the session, on the first authenticated call, before any rate
or IP behaviour was in play.

Your comments already show you half-know this — `engine_ig.build_client`'s note
about "a session moving between handsets ... the shape of a stolen cookie." The
part not yet written down is that the *browser → app* transition is itself one
of those moves, and it happens by design on every account you onboard.

Two honest options:

- **(a) Accept it and keep the volume low.** The discontinuity exists; minimise
  how many requests are made through it. This is roughly where you are.
- **(b) Do the reads in the context that minted the session.** You already have
  `engine_ig._browser_session` and a persistent Chromium profile per account.
  Moving the read path into the browser removes the discontinuity entirely, at
  the cost of a much heavier collector. This is a real architectural fork and
  should be costed, not decided casually.

**Also worth fixing regardless:** `engine_ig.build_client` calls
`login_by_sessionid` on *every construction*. `collect_ig` correctly uses
`load_client` (the reuse path, no re-login) — but `client_from_store` →
`build_client` is the CLI path, and every CLI invocation re-runs the import
handshake and resets the claim to `0`. Any operator running `resolve-ids` or a
probe by hand is paying that cost without knowing it.

### 3.4 Gap 4 — `chrome_major: 140` against a real Chromium 151

From your CHECKPOINT, filed under "Still open":

> The identities minted before Chromium 151 was installed carry
> `chrome_major: 140` (the fallback); the real browser is 151. Coherent with
> each other, not with the binary — a reseed would fix it, at the cost of a
> sign-in. Leave it.

**Reconsider this one.** `ig_identity.web_headers()`, `playwright_kwargs()` and
`cdp_user_agent_metadata()` all build from `identity.chrome_major`. So the
sign-in browser *renders* as Chrome 151 — real engine behaviour, real feature
detection, real `navigator.userAgentData` high-entropy values — while its UA
string and every Client Hint it sends say 140. That is precisely the
UA-versus-reality mismatch class that is cheap for any detector to check, and
it is the exact failure mode `ig_identity.py`'s own docstring exists to prevent.

**It does not need a reseed.** `chrome_major` is a *derived* field, not an
identifier — unlike `uuids` and `device_settings`, which must never move. You
can update it in place on the existing seeds and the handset is unchanged; only
the browser version it claims moves. Either patch the two seed files, or add a
check at sign-in that refreshes `chrome_major` (and only that) when the binary
has moved.

The counter-argument — "a browser jumping 140 → 151 in one step is itself a
change" — is real but weak: Chrome auto-updates and users do skip versions. A
browser version frozen for months across a Chrome release cycle is the stranger
signal.

### 3.5 Gap 5 — fingerprinting: where it matters, and where it does not

You wrote "I think we miss fingerprint." Partly. Be precise about where:

**You do not have a canvas / WebGL / audio problem in the collection path**,
because that path runs no JavaScript at all. Those surfaces exist only in a
browser, and your browser is used only at sign-in. Chasing canvas noise would be
effort spent on a surface Instagram never sees from your collector.

**In the sign-in browser**, what you already do is more than most: UA, viewport,
`device_scale_factor`, `is_mobile`, `has_touch`, `locale`, `timezone_id`, and —
notably — CDP `Emulation.setUserAgentOverride` with a full `userAgentMetadata`
so the Client Hints agree with the UA (`ig.py:_client_hints`). That last one is
the step almost everybody skips, and skipping it leaves `sec-ch-ua-platform`
saying `Linux` under an Android UA.

What is **not** covered in the browser:

- **WebRTC.** `launch_persistent_context` sends HTTP through the proxy; WebRTC
  negotiates its own UDP path and in most configurations is *not* tunnelled. A
  page that opens an `RTCPeerConnection` can enumerate the VPS's real public IP
  next to your Indian exit. Close it in the args list at `auth.py:426` —
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` (flag spelling is
  Chromium-version-dependent; verify on 151) — or disable the API outright.
- **`--disable-blink-features=AutomationControlled` is not enough on its own.**
  It is in your args (`auth.py:427`) and it is correct to have, but it is the
  most famous flag in this space and stopped being sufficient years ago.
  Separately, Playwright ≥ 1.53 (June 2025) changed `navigator.webdriver`
  defaults, and Chrome patched the CDP `Runtime.enable` console-getter side
  channel in V8 in May 2025 — this is a live, moving surface. **Measure it
  rather than reason about it:** load a fingerprint test page through the
  account's own proxy, using the account's own `_launch` path, and read the
  result. You have all the machinery to do that already.
- **GPU renderer.** Headless Chromium on a VPS typically reports SwiftShader.
  A Samsung Galaxy A15 does not. Your streamed sign-in door is headful, which
  is the right answer; anywhere a headless IG browser path still exists is a
  hole.

**The one fingerprint gap that is real for you is TLS in the collection path.**
`requests` presents a static, obviously-not-Chrome ClientHello and HTTP/2
SETTINGS on every call while the UA claims to be the Instagram Android app.
JA3/JA4 and Akamai's HTTP/2 fingerprinting are both well documented as
techniques (the H2 work is Akamai's own Black Hat EU 2017 whitepaper); whether
Meta specifically runs them is not publicly confirmed. `curl_cffi` or
`tls-client` can impersonate a real browser's stack. **I would rank this below
gaps 1–4**: it is the expensive fix for the least-confirmed signal, it pins you
to a browser build that goes stale every Chrome release, and one static
impersonation profile shared across every account becomes its own fleet-level
tell (many "different" users with a byte-identical JA4).

### 3.6 Gap 6 — what `ig_human.py` can and cannot buy

`ig_human.py` is genuinely good work — day plans of 2–4 sessions, log-normal
request gaps with a fat tail, longer source-switch pauses, a per-account stable
hour offset so three phones do not wake together, daily budgets, a warm-up ramp.
Keep all of it.

But be clear about what it buys. **It shapes when requests leave. It does not
change what a request looks like.** And instagrapi's challenge handler
recognises a step literally named **`scraping_warning`** — Instagram's checkpoint
system has a named category for read/harvest-shaped traffic, distinct from the
spam and login-security categories. Read-only collection at volume is explicitly
modelled. Perfect human rhythm on a client that does not look like the app is
still the wrong client, politely paced.

One tuning note: `ACTIVE_START_H=7`, `ACTIVE_END_H=24` is a **17-hour** waking
window. Two to four sessions inside 17 hours is plausible; the window itself is
wide for one person. Narrowing it costs you nothing you are actually using.

---

## 4. What I could not check

- **Production state.** This repo's `profiles/ig_device_ig_{a,b}.json` are both
  still the legacy US Pixel 8 Pro / `en_US` / `GMT-04:00` seeds from 3–5 August,
  with no `.bak` beside them, meaning no reseed has ever run *here*. The live
  accounts (`@youssefnasser168` and successors) are on the VPS with their own
  `profiles/` and `pool.db`, neither of which is in this checkout. **Run
  `ig_identity.is_legacy()` across the production seeds before assuming they are
  fine** — and note that `ig_browser_adopt`, the door that works best, does not
  itself call `_fresh_phone_if_legacy`; it relies on `ig.InteractiveLogin._phone`
  having reseeded when the window opened. That is correct today, but it is an
  invariant held in two places rather than one.
- **The actual proxy usernames** (`enc_proxy`, encrypted, correctly). Check
  whether any carry `-rotate`.
- Whether `cl.mid` matches the carried `mid` cookie on a live session (§3.2).

---

## 5. What to do, in order

Ordered by (effect × confidence) ÷ cost.

**1. Buy static residential / ISP IPs, one per account.**
Fixes `proxy_broken` at the root, removes the IP-hop signal, and makes the
30m→4h backoff mean what it says. Market rate is roughly **$0.25–$3.50 per IP
per month**, scaling on exclusivity rather than count (Webshare static
residential is the cheap end at volume; Bright Data / Decodo / IPRoyal dedicated
tiers are the expensive end). For a handful of accounts this is a rounding
error against the time already spent on it. Highest confidence, lowest effort,
and **your own CHECKPOINT already names it as the answer.**
*Caveat:* a static IP can be burned once and you do not automatically escape it
— prefer a private/dedicated tier over a shared one, and keep the ability to
request a replacement.

**2. Write session state back after every pass** (`ig_session.touch()`, §3.2).
~30 lines. Closes the frozen-claim and stale-`mid` gap. This is the highest-value
*code* change in this document.

**3. Fix `chrome_major` in place** on the live seeds, and make sign-in refresh it
when the binary has moved (§3.4). No reseed, no lost trust.

**4. Close WebRTC in the browser launch args, and verify the exit *from
Chromium*.** `proxy_check` proves the `requests` path; Chromium's proxy handling
is a separate code path with its own failure modes (`_proxy_kwargs` exists
precisely because Chromium ignores credentials in `--proxy-server`). One
navigation to an IP-echo page from inside `_launch` would prove it.

**5. Log `cl.mid` vs the carried `mid` cookie at sign-in.** One line. If they
differ, decide which one wins and set it.

**6. Record the exit IP per pass, not just per sign-in.** You already have
`meta.exit`. Extending it to a short rolling history turns "is the IP moving?"
from a suspicion into a number, and tells you whether moves are intra-ASN or
cross-ASN — which is the distinction that actually matters.

**7. Only then** take the architectural fork in §3.3 (reads in the browser vs.
accept the split), with the measurements from 1–6 in hand.

---

## 6. The door you did not ask about

Your collector reads **metrics** (counts, timestamps, permalinks) for a **fixed
watchlist of known accounts** — per `REPORT_TOOL_PLAN`, 335 IG posts and 339 FB
reels from about 15 pages, refreshed daily, by author rather than by post. That
is a narrow, well-bounded requirement, and it is close to exactly what Meta's
**Instagram Graph API** is built to serve.

Two relevant shapes:

- For accounts the agency **manages** (its own clients', with a Business or
  Creator account connected to a Facebook Page): full media insights with a
  token, no checkpoints, published rate limits you can plan against.
- For public Business/Creator accounts the agency **does not** manage: the
  `business_discovery` edge lets one authorised business account look up another
  public business account's `followers_count`, `media_count`, and per-media
  `like_count`, `comments_count`, `timestamp`, `caption`, `permalink` — without
  the watched account authorising anything. That is very nearly the exact
  payload `engine_ig` is currently scraping for.

Limits are real (public Business/Creator accounts only — no personal or private
accounts; standard IG Graph rate limits; an app review for the insights
permissions) and **my knowledge of Meta's platform has a cutoff, so verify the
current shape in Meta's docs before planning around it.** But the cost is a
conversation with each client and an app review, versus an open-ended arms
race against a detector you cannot see. For the fraction of the watchlist that
is business accounts, this is worth costing out properly **before** spending
more on evasion.

---

## 7. The honest ceiling

None of the above makes the collector undetectable, and it would be dishonest to
imply otherwise. What it does is remove **specific, checkable discontinuities** —
a frozen claim, a device identity that disagrees with itself, an IP that hops
ASNs, a UA that disagrees with the binary rendering it. Instagram runs a scoring
system over many features simultaneously (the one primary source on Meta's
architecture, the Facebook Immune System paper, describes a real-time rule + ML
engine doing per-action classification over device, IP, behavioural and
social-graph features together). It is not a checklist of gates you can tick off
and be finished.

Two consequences worth holding onto:

- **A named `scraping_warning` challenge step exists.** Read-only collection at
  volume is its own modelled risk category. Being gentle helps. Being invisible
  is not on offer.
- **Fixing the four gaps above changes your failure rate, not your failure
  mode.** Plan for accounts to be lost and replaced. `guard.py`, the quarantine
  ladder and `failover()` are the parts of this system that assume that, and
  they are the parts most likely to still be right in a year.

---

## Appendix — evidence quality

The research behind §3 is graded, because most of what is written publicly on
this topic is proxy-vendor marketing.

**Documented** (primary source, verifiable):
- `datr` — Meta's own cookie-policy language, quoted verbatim in Dimova et al.,
  WPES 2022: identifies browsers "for purposes of security and site integrity,
  including for account recovery", 2-year lifetime.
- Facebook Immune System — Meta-authored paper describing the rule+ML
  architecture (per-action classification over device/IP/behaviour/graph).
- HTTP/2 client fingerprinting — Akamai's Black Hat EU 2017 whitepaper (SETTINGS
  values and order, WINDOW_UPDATE, PRIORITY frames, pseudo-header order).
- JA3/JA4 — public specs and tooling.
- Webshare proxy username syntax, static-residential tiering, auto-replacement
  policy, IP-authorization port cap — Webshare's own API docs and help centre.
- Decodo's sticky-session mechanics and the "peer goes offline" caveat.
- instagrapi's error taxonomy, `challenge.py`'s step names (including
  `scraping_warning`), and `auth.py`'s `login_by_sessionid` implementation — the
  library's own source and maintainer-authored docs.
- instagrapi Best Practices naming per-request proxy switching as an
  anti-pattern.
- Microsoft Entra ID Protection's "unfamiliar sign-in properties" / "atypical
  travel" detectors — as a documented example of *the class of system*, not of
  Meta's.
- Cloudflare's 2025 CGNAT research (CGNAT IPs see ~3× more rate-limiting despite
  similar bot rates — which cuts *against* the vendor line that mobile IPs are
  inherently safer).

**Corroborated** (multiple independent reimplementations agree; Meta silent):
- `mid` as a persistent machine identifier that should be carried across logins.
- `X-IG-WWW-Claim`'s echo lifecycle (`x-ig-set-www-claim` → store → resend,
  default `"0"`).
- `X-IG-App-ID` (`936619743392459` for the web surface) being validated.
- `logging_client_events` as real, batched, undecoded client telemetry that
  multiple maintainers independently believe matters.
- `login_by_sessionid` producing `challenge_required` shortly after import
  (`subzeroid/instagrapi#2377`).

**Folklore / vendor marketing** (treat as directional at best):
- All specific rate-limit numbers (150/hr for `user_info`, 200/hr per IP, etc.)
  — every one traces to a proxy or scraping vendor's blog. **Notably, the common
  claim that profile lookups are gated hardest is *not* supported** by the one
  vendor dataset that gives numbers; it puts profile and feed reads in the same
  range. Do not plan around either figure.
- The `rur` cookie's meaning. No reliable public source exists. If you have
  decoded it empirically, your own observation beats anything findable.
- `shbid` / `shbts` — nothing substantive found.
- "Mobile / CGNAT IPs are treated more leniently" — ubiquitous in vendor copy,
  and the only rigorous independent measurement (Cloudflare, above) points the
  other way.
- Any claim that Meta licenses a named third-party IP-reputation feed (IPQS,
  Spur, Scamalytics, IP2Proxy). Unverified.
- `falco` / `pigeon` as Meta client-telemetry codenames — no reliable public
  source; likely a conflation. Do not repeat them as fact.
- Whether Meta runs canvas/WebGL/audio fingerprinting on the login surface
  specifically. The *technique* is textbook; Meta's use of it is unconfirmed.
