// Account Control Panel — manage the scraper accounts of all three platforms
// from one place: add / edit / remove, see status, promote a backup, force
// failover, refresh backup codes, preview the current TOTP, and (next step)
// sign in on the server IP.
//
// TWO things show per platform, on purpose:
//   1. The managed POOL (store_accounts) — full controls.
//   2. LIVE sessions already running that aren't in the pool yet — read from
//      the existing X / Instagram status endpoints so nothing you already run
//      ever disappears from this page. Each has an "Add to pool" button to
//      bring it under management.
import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, fmtAgo, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";

const PLATS = [["x", "X"], ["ig", "Instagram"], ["fb", "Facebook"]];
const BADGE = { x: "platform-x", ig: "platform-ig", fb: "platform-fb" };
const BADGE_TXT = { x: "X", ig: "IG", fb: "FB" };

const STATUS = {
  active: { dot: "", text: "Active", cls: "st-good", chip: "good" },
  backup: { dot: " off", text: "Backup", cls: "", chip: "" },
  needs_login: { dot: " warn", text: "Needs login", cls: "st-warn", chip: "warn" },
  quarantined: { dot: " warn", text: "Quarantined", cls: "st-warn", chip: "warn" },
  dead: { dot: " bad", text: "Dead", cls: "st-crit", chip: "crit" },
};

// ---------------------------------------------------------------------------
// Add / edit / backup-code modals
// ---------------------------------------------------------------------------

function AddModal({ initial, onDone, onClose }) {
  const [f, setF] = useState({
    platform: "x", label: "", login: "", password: "",
    totp_secret: "", backup_codes: "", proxy_id: "", proxy_url: "", notes: "",
    ...(initial || {}),
  });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const save = async () => {
    setBusy(true); setErr("");
    try {
      await api.poolAdd({ ...f, proxy_id: f.proxy_id || null });
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={initial?.label ? `Add “${initial.label}” to the pool` : "Add account"} onClose={onClose}
           sub="Enters the pool as a warm backup. Secrets are encrypted at rest.">
      <div className="field">
        <label>Platform</label>
        <select value={f.platform} onChange={set("platform")}>
          <option value="x">X (Twitter)</option>
          <option value="ig">Instagram</option>
          <option value="fb">Facebook</option>
        </select>
      </div>
      <div className="field">
        <label>Label</label>
        <input value={f.label} autoFocus onChange={set("label")} placeholder="e.g. fb_backup_2" />
      </div>
      <div className="field">
        <label>Login (username / email)</label>
        <input value={f.login} onChange={set("login")} placeholder="account@example.com" />
      </div>
      <div className="field">
        <label>Password</label>
        <input type="password" value={f.password} onChange={set("password")}
               placeholder="stored encrypted" />
      </div>
      <div className="field">
        <label>TOTP secret (authenticator setup key)</label>
        <input value={f.totp_secret} onChange={set("totp_secret")}
               placeholder="paste the setup key — spaces ok" />
      </div>
      <div className="field">
        <label>Backup codes — one per line (optional)</label>
        <textarea rows="3" value={f.backup_codes} onChange={set("backup_codes")}
                  placeholder={"11112222\n33334444"} />
      </div>
      <div className="field">
        <label>Proxy / IP id (a label, optional)</label>
        <input value={f.proxy_id} onChange={set("proxy_id")} placeholder="e.g. resi-in-01" />
      </div>
      <div className="field">
        <label>Residential proxy URL — username &amp; password go INLINE</label>
        <input value={f.proxy_url} onChange={set("proxy_url")}
               placeholder="http://user:pass@gateway.host:port" />
        <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 5, lineHeight: 1.5 }}>
          The whole credential is one URL — both username and password sit
          inside it. Stored encrypted, never shown again. For a sticky IP,
          use your provider's session-suffixed username (e.g.
          <code>user-session-ig1</code>). Instagram must run through this, not
          the server IP.
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !f.label.trim() || !f.login.trim()}
                onClick={save}>Add to pool</button>
      </div>
    </Modal>
  );
}

function EditModal({ a, onDone, onClose }) {
  const [f, setF] = useState({
    label: a.label, login: a.login, password: "", totp_secret: "",
    proxy_id: a.proxy_id || "", proxy_url: "", notes: a.notes || "",
  });
  const [dropProxy, setDropProxy] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const save = async () => {
    setBusy(true); setErr("");
    try {
      const body = { account_id: a.account_id, label: f.label, login: f.login,
                     proxy_id: f.proxy_id || null, notes: f.notes };
      if (f.password) body.password = f.password;          // blank = keep
      if (f.totp_secret) body.totp_secret = f.totp_secret; // blank = keep
      // The proxy URL is WRITE-ONLY: it is encrypted at rest and never sent back
      // to this page, so a blank box has to mean KEEP, exactly like password and
      // TOTP. Clearing it must be said out loud — otherwise a label rename would
      // silently drop the account back onto the server IP, which is the
      // sign-in/collect fingerprint mismatch ACCOUNTS.md 7 exists to prevent.
      if (dropProxy) body.proxy_url = "";
      else if (f.proxy_url.trim()) body.proxy_url = f.proxy_url.trim();
      await api.poolUpdate(body);
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`Edit ${a.label}`} onClose={onClose}
           sub="Leave password / TOTP / proxy URL blank to keep the current one.">
      <div className="field"><label>Label</label>
        <input value={f.label} onChange={set("label")} /></div>
      <div className="field"><label>Login</label>
        <input value={f.login} onChange={set("login")} /></div>
      <div className="field"><label>New password (blank = keep)</label>
        <input type="password" value={f.password} onChange={set("password")} /></div>
      <div className="field"><label>New TOTP secret (blank = keep)</label>
        <input value={f.totp_secret} onChange={set("totp_secret")} /></div>
      <div className="field"><label>Proxy / IP id (a label, optional)</label>
        <input value={f.proxy_id} onChange={set("proxy_id")} placeholder="e.g. resi-in-01" /></div>
      <div className="field">
        <label>
          Residential proxy URL — username &amp; password go INLINE
          {a.has_proxy ? " (blank = keep the one on file)" : ""}
        </label>
        <input value={f.proxy_url} onChange={set("proxy_url")} disabled={dropProxy}
               placeholder={a.has_proxy
                 ? "a proxy is on file — type a new URL to replace it"
                 : "http://user:pass@gateway.host:port"} />
        <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 5, lineHeight: 1.5 }}>
          {a.has_proxy
            ? "A proxy is stored for this account. It is encrypted and never sent back to this page, so it cannot be shown — type a new URL to replace it."
            : "No proxy: this account signs in and collects from the SERVER IP. Two accounts sharing one address correlate to a single operator, so a ban on one raises suspicion on the other."}
          {" "}The whole credential is one URL — both username and password sit
          inside it. For a sticky IP, use your provider's session-suffixed
          username (e.g. <code>user-session-x1</code>). It takes effect on this
          account's NEXT sign-in, which is what writes it through to the
          collector.
        </div>
        {a.has_proxy && (
          <label style={{ display: "flex", alignItems: "center", gap: 7, marginTop: 8,
                          fontSize: 12, color: "var(--ink-3)" }}>
            <input type="checkbox" checked={dropProxy}
                   onChange={(e) => setDropProxy(e.target.checked)} />
            Remove the stored proxy — this account goes back to the server IP
          </label>
        )}
      </div>
      <div className="field"><label>Notes</label>
        <textarea rows="2" value={f.notes} onChange={set("notes")}
                  placeholder="e.g. bought 2026-08-14 · warm since 2026-08-20" /></div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !f.label.trim()} onClick={save}>
          Save changes
        </button>
      </div>
    </Modal>
  );
}

function CodesModal({ a, onDone, onClose }) {
  const [codes, setCodes] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true); setErr("");
    try {
      await api.poolBackupCodes(a.account_id, codes);
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`Backup codes — ${a.label}`} onClose={onClose}
           sub={`${a.backup_codes_left} unused now. Pasting a new set REPLACES the old one.`}>
      <div className="field">
        <label>One-time recovery codes — one per line</label>
        <textarea rows="6" value={codes} autoFocus onChange={(e) => setCodes(e.target.value)}
                  placeholder={"paste the fresh set from the account's 2FA settings"} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !codes.trim()} onClick={save}>
          Save codes
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Sign in — one path for all three platforms, and it is not a browser
// ---------------------------------------------------------------------------
//
// Two mechanisms, ranked by how much suspicion they create (signin.py has the
// full reasoning):
//
//   IMPORT     paste the cookies from a browser you are already signed into.
//              No login event ever reaches the platform from this server — no
//              form to fingerprint, no captcha to lose. Works on all three.
//   BACKGROUND Instagram only, via instagrapi's app API. Not a browser, which
//              is why it works where the streamed window never did. Costs one
//              real login, but it is the only path that can re-login by itself
//              when a session dies.
//
// The server runs it in a thread and streams its commentary back, because
// "Instagram wants a code sent to your email" and "that cookie expired" are
// different problems and a lone red X cannot tell them apart.

const NEEDS_HINT = {
  proxy: "The residential proxy is missing or its exit is unusable. Fix it on this card (Edit → proxy URL — another session number if the exit is dead), then sign in again.",
  totp: "Add the account's TOTP secret on this card (Edit → TOTP secret), or paste a session instead.",
  paste: "Paste a session from a browser you are already signed into.",
  browser: "Open this account's own browser below (its phone, its proxy), clear what Instagram asks, and the session is adopted for you.",
};

// ---------------------------------------------------------------------------
// The streamed browser window — the account's OWN Chromium on the server
// ---------------------------------------------------------------------------
//
// Instagram sometimes wants to see a browser it recognises (a native
// checkpoint, a Bloks flow, a captcha). The server opens one per account:
// shaped like the account's phone (ig_identity), through the account's own
// proxy, on a persistent profile — and streams it here. The operator clicks
// and types; when the page is signed in, the session is adopted by the app
// client on the same phone (signin.ig_browser_adopt) and the window closes.
function BrowserLoginModal({ a, onDone, onClose }) {
  const [win, setWin] = useState(null);       // {width,height,phone,...}
  const [state, setState] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [tick, setTick] = useState(0);
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(true);
  const [done, setDone] = useState(null);
  const imgRef = React.useRef(null);

  useEffect(() => {
    let alive = true;
    api.loginStart(a.account_id).then((r) => {
      if (!alive) return;
      setWin(r); setState(r.state); setName(r.screen_name || ""); setUrl(r.url || "");
      setBusy(false);
      if (r.warning) setErr(r.warning);
    }).catch((e) => { if (alive) { setErr(String(e.message || e)); setBusy(false); } });
    const t = setInterval(() => setTick((n) => n + 1), 1200);
    return () => { alive = false; clearInterval(t); api.loginCancel().catch(() => {}); };
  }, [a.account_id]);

  const act = async (body) => {
    if (busy) return;
    setBusy(true); setErr("");
    try {
      const r = await api.loginAct(body);
      setState(r.state); setName(r.screen_name || ""); setUrl(r.url || "");
      if (r.captured) { setDone(r); onDone(); }
      else if (r.closed) setErr("The window closed.");
    } catch (e) { setErr(String(e.message || e)); }
    setBusy(false);
  };

  const click = (ev) => {
    if (!win || !imgRef.current) return;
    const box = imgRef.current.getBoundingClientRect();
    const x = Math.round((ev.clientX - box.left) * (win.width / box.width));
    const y = Math.round((ev.clientY - box.top) * (win.height / box.height));
    act({ act: "click", x, y });
  };

  const sub = win
    ? `${win.phone ? win.phone + " · " : ""}${win.width}×${win.height} · through this account's proxy`
    : "opening the account's browser on the server…";

  return (
    <Modal title={`${a.label} — this account's browser`} onClose={onClose} sub={sub}>
      <div style={{ display: "flex", gap: 14, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ flex: "0 0 auto", border: "1px solid var(--line)", borderRadius: 10,
                      overflow: "hidden", background: "#000",
                      width: win ? Math.min(win.width, 380) : 380 }}>
          {win ? (
            <img ref={imgRef} src={`/api/login/frame?t=${tick}`} alt="the sign-in window"
                 onClick={click} draggable={false}
                 style={{ display: "block", width: "100%", cursor: busy ? "progress" : "pointer",
                          aspectRatio: `${win.width} / ${win.height}` }} />
          ) : (
            <div style={{ padding: 40, color: "#bbb", fontSize: 13 }}>
              {err || "starting Chromium as this phone…"}
            </div>
          )}
        </div>
        <div style={{ flex: "1 1 260px", minWidth: 240 }}>
          <div className="kv"><span>state</span>
            <b className={state === "logged_in" ? "st-good" : state === "challenge" ? "st-crit" : ""}>
              {state || "…"}{name ? ` · @${name}` : ""}
            </b>
          </div>
          {url && <div className="kv"><span>page</span><b style={{ fontWeight: 400, wordBreak: "break-all" }}>{url}</b></div>}
          <div className="field" style={{ marginTop: 8 }}>
            <label>Type into the page</label>
            <div style={{ display: "flex", gap: 6 }}>
              <input value={text} onChange={(e) => setText(e.target.value)}
                     placeholder="click a field in the frame first, then type here"
                     onKeyDown={(e) => { if (e.key === "Enter") { act({ act: "type", text }); setText(""); } }} />
              <button className="btn" disabled={busy || !text}
                      onClick={() => { act({ act: "type", text }); setText(""); }}>Type</button>
            </div>
          </div>
          <div className="cactions" style={{ marginTop: 6 }}>
            <button disabled={busy} onClick={() => act({ act: "key", key: "Enter" })}>Enter</button>
            <button disabled={busy} onClick={() => act({ act: "key", key: "Tab" })}>Tab</button>
            <button disabled={busy} onClick={() => act({ act: "key", key: "Backspace" })}>⌫</button>
            <button disabled={busy} onClick={() => act({ act: "scroll", dy: 400 })}>Scroll ↓</button>
            <button disabled={busy} onClick={() => act({ act: "scroll", dy: -400 })}>Scroll ↑</button>
            <button disabled={busy} onClick={() => act({ act: "reload" })}>Reload</button>
            <button disabled={busy} onClick={() => act({ act: "noop" })}>Check state</button>
          </div>
          <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.55, marginTop: 10 }}>
            Sign in as you would on a phone. Whatever Instagram asks — a code, a captcha,
            "confirm it's you" — answer it here. The moment the page is signed in, the
            session is adopted by the collector on this same phone and this window closes.
          </div>
          {done && (
            <div className={done.active ? "banner-ok" : "banner-crit"} style={{ marginTop: 10 }}>
              <b>{done.active ? "Signed in and adopted." : "Signed in, but not adopted."}</b>{" "}
              {done.username ? `@${done.username}. ` : ""}{done.detail}
            </div>
          )}
          {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
        </div>
      </div>
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>{done ? "Done" : "Close window"}</button>
      </div>
    </Modal>
  );
}

function SignInModal({ a, onDone, onClose }) {
  const [help, setHelp] = useState(null);
  const [blob, setBlob] = useState("");
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState([]);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState("");
  const [waiting, setWaiting] = useState(null);   // {choice, hint} while a code is wanted
  const [code, setCode] = useState("");
  const [browser, setBrowser] = useState(false);

  const canBackground = a.platform === "ig";
  const h = help?.[a.platform];

  const sendCode = async () => {
    try { await api.loginCode(code); setCode(""); setWaiting(null); }
    catch (e) { setErr(String(e.message || e)); }
  };

  useEffect(() => {
    api.poolSigninHelp().then((r) => setHelp(r.platforms)).catch(() => {});
  }, []);

  // Poll while it runs. The server keeps one sign-in at a time, so there is
  // exactly one job to watch and no id to track.
  const watch = () => {
    const tick = async () => {
      try {
        const r = await api.poolSigninStatus();
        setLines(r.lines || []);
        setWaiting(r.waiting_for || null);
        if (!r.running) {
          setBusy(false);
          setWaiting(null);
          if (r.result) setResult(r.result);
          onDone();
          return;
        }
      } catch (e) { setErr(String(e.message || e)); setBusy(false); return; }
      setTimeout(tick, 1200);
    };
    setTimeout(tick, 700);
  };

  const start = async (mode) => {
    setBusy(true); setErr(""); setResult(null); setLines([]);
    try {
      await api.poolSignin({ account_id: a.account_id, mode, cookies: blob });
      watch();
    } catch (e) { setErr(String(e.message || e)); setBusy(false); }
  };

  return (
    <Modal title={`Sign in — ${a.label}`} onClose={onClose}
           sub="Pasting a session from your own browser is the safest path: no login ever happens from this server.">
      {canBackground && (
        <div className="field">
          <label>Background sign-in (no browser)</label>
          <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.55, marginBottom: 8 }}>
            Uses the password and TOTP stored on this card, through this
            account’s residential proxy, over Instagram’s app API. Costs one
            real login — but it is the only path that can refresh itself later
            without you.
          </div>
          <button className="btn btn-brand" disabled={busy}
                  onClick={() => start("auto")}>
            {busy ? "Signing in…" : "Sign in in the background"}
          </button>
          {waiting && (
            <div className="banner-warn" style={{ marginTop: 10 }}>
              <b>Instagram sent a one-time code to your {waiting.choice}{waiting.hint ? ` (${waiting.hint})` : ""}.</b>
              <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="6-digit code"
                       inputMode="numeric" autoFocus
                       onKeyDown={(e) => { if (e.key === "Enter" && code.trim()) sendCode(); }} />
                <button className="btn btn-brand" disabled={!code.trim()} onClick={sendCode}>Send code</button>
              </div>
              <div style={{ color: "var(--ink-3)", fontSize: 12, marginTop: 6 }}>
                Five minutes before the attempt lapses. This is the whole reason a sign-in
                from the server used to fail: the code had nowhere to go.
              </div>
            </div>
          )}
        </div>
      )}

      {canBackground && (
        <div className="field">
          <label>…or open this account's browser</label>
          <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.55, marginBottom: 8 }}>
            A real Chromium on the server, shaped like this account's phone, through its
            own proxy, streamed here. For the walls only a browser can clear — a captcha,
            "confirm it's you", a native checkpoint. The session it earns is adopted by
            the collector on the same phone.
          </div>
          <button className="btn" disabled={busy} onClick={() => setBrowser(true)}>
            Open this account's browser
          </button>
          {browser && <BrowserLoginModal a={a} onDone={onDone} onClose={() => setBrowser(false)} />}
        </div>
      )}

      <div className="field">
        <label>{canBackground ? "…or paste a session" : "Paste a session"}</label>
        {h && (
          <div style={{ color: "var(--ink-3)", fontSize: 12.5, lineHeight: 1.55, marginBottom: 8 }}>
            {h.how}
            <div style={{ marginTop: 6 }}>
              Needs <b>{h.required.join(" + ")}</b>. Paste everything — {" "}
              <b>{h.valuable.slice(0, 2).join(", ")}</b> and the rest are the
              device tokens that keep this looking like a machine{" "}
              {h.where} already knows.
            </div>
          </div>
        )}
        <textarea rows="5" value={blob} onChange={(e) => setBlob(e.target.value)}
                  placeholder={'paste the cookies — "Copy all as JSON", the cookie: header line, or name=value lines'} />
        <button className="btn btn-brand" style={{ marginTop: 8 }}
                disabled={busy || !blob.trim()} onClick={() => start("paste")}>
          {busy ? "Checking…" : "Import this session"}
        </button>
      </div>

      {lines.length > 0 && (
        <div className="field">
          <label>Progress</label>
          <div style={{ fontFamily: "ui-monospace, monospace", fontSize: 12,
                        lineHeight: 1.6, color: "var(--ink-2)",
                        maxHeight: 160, overflowY: "auto",
                        border: "1px solid var(--line)", borderRadius: 8, padding: "8px 10px" }}>
            {lines.map((l, i) => <div key={i}>{l}</div>)}
          </div>
        </div>
      )}

      {result && (
        <div className={result.ok ? "banner-ok" : "banner-crit"}
             style={{ marginTop: 4 }}>
          <b>{result.ok ? "Signed in." : "Not signed in."}</b> {result.detail}
          {!result.ok && NEEDS_HINT[result.needs] && (
            <div style={{ marginTop: 6 }}>{NEEDS_HINT[result.needs]}</div>
          )}
        </div>
      )}
      {err && <div className="err">{err}</div>}

      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>
          {result?.ok ? "Done" : "Close"}
        </button>
      </div>
    </Modal>
  );
}


// ---------------------------------------------------------------------------
// Session state — ALWAYS rendered, on every card
// ---------------------------------------------------------------------------
//
// This row used to be conditional on `live` (`{live && ...}`), and that single
// `&&` is why an Instagram card could show no state at all: an account that is
// in the pool but has never been signed in on the server has no row in
// ig_accounts.db, so `liveFor` returned null, so the card silently rendered
// nothing between "proxy / IP" and "last success —". Two very different
// situations — "signed in and collecting" and "we have never seen this account
// sign in" — looked identical: blank.
//
// A missing session IS a state, and the most important one, because it is the
// only one an operator has to act on. So the row is unconditional and says
// which of the two it is.
function SessionRow({ a, live }) {
  // No live record at all: the account exists in the pool and nowhere else.
  if (!live) {
    return (
      <div className="kv"><span>session</span>
        <b className="st-warn">
          never signed in on this server
          <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>
            {" · use Login now"}
          </span>
        </b>
      </div>
    );
  }

  // Instagram parks a checkpoint tombstone next to the session. It outranks
  // active/inactive: the session may still half-work while a human is required.
  if (live.checkpoint_at) {
    return (
      <div className="kv"><span>session</span>
        <b className="st-crit">
          checkpoint — Instagram wants a human ({fmtAgo(live.checkpoint_at)})
        </b>
      </div>
    );
  }

  const bits = [];
  if (live.requests != null) bits.push(`${live.requests} requests`);
  if (live.last_used) bits.push(`last used ${fmtAgo(live.last_used)}`);

  return (
    <>
      <div className="kv"><span>session</span>
        <b className={live.active ? "st-good" : "st-crit"}>
          {live.active ? "signed in · collecting" : "signed in once · not working now"}
          {bits.length ? (
            <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>
              {` · ${bits.join(" · ")}`}
            </span>
          ) : null}
        </b>
      </div>
      {live.error && (
        <div className="kv"><span>session error</span>
          <b className="st-crit">{live.error}</b></div>
      )}
    </>
  );
}


// ---------------------------------------------------------------------------
// A managed (pool) account card — full controls
// ---------------------------------------------------------------------------

function AccountCard({ a, live, onChanged }) {
  const [msg, setMsg] = useState("");
  const [modal, setModal] = useState(null);
  const s = STATUS[a.status] || STATUS.backup;

  const act = async (fn, okMsg) => {
    setMsg("…");
    try {
      const r = await fn();
      if (r && r.ok === false && r.todo) setMsg(r.todo);
      else if (r && r.note) setMsg(r.note);
      else setMsg(okMsg || "done");
      onChanged();
    } catch (e) { setMsg(String(e.message || e)); }
  };

  const remove = () => {
    if (!confirm(`Remove ${a.label}? This deletes the account from the pool.`)) return;
    act(() => api.poolRemove(a.account_id), "removed");
  };
  const promote = () => act(() => api.poolPromote(a.account_id), "promoted to active");
  const quarantine = () => act(() => api.poolStatus(a.account_id, "quarantined"), "quarantined");
  const revive = () => act(() => api.poolStatus(a.account_id, "backup"), "returned to pool");
  const showCode = async () => {
    setMsg("…");
    try { const r = await api.poolTotp(a.account_id); setMsg(r.code ? `TOTP now: ${r.code}` : "no TOTP set"); }
    catch (e) { setMsg(String(e.message || e)); }
  };
  // Instagram: N accounts collect in parallel. "Bench" takes this one off
  // the roster (its unpinned sources move to the others on the next pass);
  // "Collect" puts it back. "New phone" mints a fresh identity — the session
  // dies with the old phone, so it benches the account until a sign-in.
  const isIg = a.platform === "ig";
  const bench = () => act(() => api.igAccount(live.username, false), "benched — its sources move on the next pass");
  const collect = () => act(() => api.igAccount(live.username, true), "collecting again from the next pass");
  const newPhone = () => {
    if (!confirm(`Mint a NEW phone for ${a.label}? The current session dies with the old phone; you will sign in again on the new one.`)) return;
    act(() => api.igReseed(live.username), "new phone minted — sign in again");
  };

  return (
    <div className="panel">
      <div className="phead" style={{ alignItems: "center", flexWrap: "wrap", rowGap: 6 }}>
        <h3>
          <span className={`dot${s.dot}`} style={{ display: "inline-block", marginRight: 9 }} />
          {a.label}
        </h3>
        <span className={`badge ${BADGE[a.platform]}`}>{BADGE_TXT[a.platform]}</span>
        <span className={`chip ${s.chip}`}>{s.text}</span>
        <span className="right">
          {a.has_proxy ? "proxied" : "no proxy"} · {a.has_totp ? "TOTP" : "no 2FA"} · {a.backup_codes_left} codes
        </span>
      </div>

      <div className="kv"><span>login</span><b>{a.login}</b></div>
      <div className="kv"><span>proxy / IP</span><b>{a.proxy_id || "none (server IP)"}</b></div>
      <SessionRow a={a} live={live} />
      {isIg && live && (
        <>
          <div className="kv"><span>phone</span>
            <b className={live.identity?.legacy ? "st-warn" : ""} style={{ fontWeight: live.identity ? 500 : 400 }}>
              {live.identity ? live.identity.text : "no phone minted yet — the first sign-in mints one"}
            </b>
          </div>
          <div className="kv"><span>collecting</span>
            <b className={live.active && !live.checkpoint_at ? "st-good" : "st-warn"}>
              {live.active ? `yes · owns ${live.owns ?? 0} source(s)` : "benched"}
              {live.exit?.exit_ip ? (
                <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>
                  {` · signed in via ${live.exit.exit_ip}${live.exit.country ? ` (${live.exit.country})` : ""}`}
                </span>
              ) : null}
            </b>
          </div>
        </>
      )}
      <div className="kv"><span>last success</span>
        <b>{a.last_success_at ? fmtAgo(a.last_success_at) : "never"}</b></div>
      {a.health && <div className="kv"><span>health</span><b className="st-warn">{a.health}</b></div>}
      {msg && <div className="kv"><span>note</span><b>{msg}</b></div>}

      <div className="cactions">
        {a.status !== "active" && <button onClick={promote}>Promote</button>}
        <button onClick={() => setModal("signin")}>Sign in</button>
        <button onClick={showCode}>Show TOTP</button>
        <button onClick={() => setModal("edit")}>Edit</button>
        <button onClick={() => setModal("codes")}>Codes ({a.backup_codes_left})</button>
        {isIg && live && (live.active
          ? <button onClick={bench}>Bench</button>
          : <button onClick={collect}>Collect</button>)}
        {isIg && live && <button onClick={newPhone}>New phone</button>}
        {a.status !== "quarantined" && a.status !== "dead"
          ? <button onClick={quarantine}>Quarantine</button>
          : <button onClick={revive}>Return to pool</button>}
        <button onClick={remove} style={{ color: "var(--critical)" }}>Remove</button>
      </div>

      {modal === "signin" && <SignInModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
      {modal === "edit" && <EditModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
      {modal === "codes" && <CodesModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// A live session that isn't in the pool yet — read-only + "Add to pool"
// ---------------------------------------------------------------------------

function OrphanCard({ r, platform, onAdopt }) {
  const name = r.username || r.label || "(unknown)";
  const active = !!r.active;
  return (
    <div className="panel" style={{ borderStyle: "dashed" }}>
      <div className="phead">
        <h3>
          <span className={`dot${active ? "" : " bad"}`} style={{ display: "inline-block", marginRight: 9 }} />
          {name}
        </h3>
        <span className={`badge ${BADGE[platform]}`} style={{ marginLeft: 4 }}>{BADGE_TXT[platform]}</span>
        <b style={{ marginLeft: 8, fontSize: 12.5, color: "var(--ink-3)" }}>Live · not in pool</b>
        <span className="right">
          {r.proxy ? "proxied" : "no proxy"}{r.requests != null ? ` · ${r.requests} requests` : ""}
        </span>
      </div>
      <div className="kv"><span>session</span>
        <b className={active ? "st-good" : "st-crit"}>{active ? "signed in · collecting" : "not signed in"}</b>
      </div>
      {(r.reasons || []).map((x, i) => (<div className="kv" key={i}><span>note</span><b>{x}</b></div>))}
      {r.error && <div className="kv"><span>error</span><b className="st-crit">{r.error}</b></div>}
      <div className="cactions">
        <button onClick={() => onAdopt({ platform, label: r.label || name, login: r.username || r.label || "" })}>
          Add to pool
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One platform section: pool accounts, then live-not-in-pool sessions
// ---------------------------------------------------------------------------

function PlatformSection({ platform, title, summary, accounts, orphans, liveFor, onAdopt, onChanged }) {
  const failover = async () => {
    if (!confirm(`Force failover on ${title}? The active account is quarantined and the next backup takes over.`)) return;
    const proxy = prompt("Fresh proxy/IP id for the promoted account (recommended — leave blank to keep its own):", "");
    try {
      const r = await api.poolFailover(platform, proxy || null);
      alert(r.promoted ? `Promoted ${r.promoted}.${r.note ? " " + r.note : ""}` : "No backup available to promote.");
      onChanged();
    } catch (e) { alert(String(e.message || e)); }
  };

  const nothing = accounts.length === 0 && orphans.length === 0;
  const parallel = platform === "ig";

  return (
    <>
      <div className="feed-head" style={{ marginTop: 18 }}>
        <h2>{title}</h2>
        <span className="right">
          {summary.active ? <>active: <b>{summary.active}</b> · </> : "no active account · "}
          {summary.backups} backup{summary.backups === 1 ? "" : "s"}
          {summary.active && summary.backups > 0 && (
            <button className="btn btn-ghost btn-sm" style={{ marginLeft: 10 }} onClick={failover}>
              Force failover
            </button>
          )}
        </span>
      </div>

      {parallel && accounts.length > 0 && (
        <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "-6px 0 10px", lineHeight: 1.5 }}>
          Instagram accounts collect <b>in parallel</b>: every card marked “collecting” owns a
          share of the sources on its own phone, through its own proxy. Promote adds a
          collector; Bench takes one off without losing its session.
        </div>
      )}

      {summary.low && accounts.length > 0 && (
        <div className="banner-crit" style={{ borderLeftColor: "var(--warning)" }}>
          <b style={{ color: "var(--warn-text)" }}>Pool low.</b> Only {summary.backups} backup
          {summary.backups === 1 ? "" : "s"} left for {title} — add another so a ban never causes an outage.
        </div>
      )}

      {nothing && (
        <Empty title={`No ${title} accounts yet`}>Use “Add account” to put one in the pool.</Empty>
      )}

      {accounts.length > 0 && (
        <div className="cards-grid">
          {accounts.map((a) => (
            <AccountCard key={a.account_id} a={a} live={liveFor(a)} onChanged={onChanged} />
          ))}
        </div>
      )}

      {orphans.length > 0 && (
        <>
          <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "2px 0 8px" }}>
            Already running — not managed here yet. “Add to pool” brings them under
            failover &amp; 2FA.
          </div>
          <div className="cards-grid">
            {orphans.map((r, i) => (
              <OrphanCard key={r.label || r.username || i} r={r} platform={platform} onAdopt={onAdopt} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// The Fix panel — what the decider (decider.py) needs a human for.
// ---------------------------------------------------------------------------
//
// A Telegram ping links here with ?fix=<condition id>. The panel shows every
// open condition, the one linked first and expanded, with the steps the
// policy wrote for it and ONLY the actions the policy allows. Nothing here
// invents advice: `steps` and `actions` come from the rule table, so the
// dashboard and the phone always say the same thing.

const KIND_TXT = {
  checkpoint: "Checkpoint — Instagram wants a human",
  no_sources: "Nothing to collect",
  session_missing: "No saved session",
  session_rejected: "Session rejected",
  unresolved_source: "Handle needs its numeric id",
  lookup_throttled: "Name lookups refused — held",
  proxy_broken: "Proxy broken — nothing reaches Instagram",
  rate_limited: "Rate-limited — backing off by itself",
  pass_error: "Collector crashing",
  paused: "Paused from the dashboard",
  budget_spent: "Daily budget spent — resting",
};

function FixCard({ c, focus, accounts, onAdopt, onChanged, onSignin }) {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(focus);
  const [pk, setPk] = useState("");
  useEffect(() => { if (focus) setOpen(true); }, [focus]);

  const norm = (v) => String(v || "").trim().toLowerCase().replace(/^@/, "");
  const pooled = c.account
    ? accounts.find((a) => a.platform === "ig"
        && (norm(a.login) === norm(c.account) || norm(a.label) === norm(c.account)))
    : null;

  const run = async (body, okText) => {
    setBusy(true); setMsg("…");
    try {
      const r = await api.deciderAction({ id: c.id, ...body });
      setMsg(okText ? okText(r) : "done");
      onChanged();
    } catch (e) { setMsg(String(e.message || e)); } finally { setBusy(false); }
  };

  const snoozed = c.snoozed_until_ms > Date.now();
  const cls = c.level === "error" ? "banner-crit" : c.level === "warn" ? "banner-warn" : "banner-ok";
  const sources = c.meta?.sources || [];

  return (
    <div className={cls} style={{ marginBottom: 10, borderLeftWidth: focus ? 6 : undefined }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", cursor: "pointer" }}
           onClick={() => setOpen(!open)}>
        <b>{KIND_TXT[c.kind] || c.kind}</b>
        {c.account && <span className="chip warn">@{c.account}</span>}
        {c.source && c.source !== "lookups" && <span className="chip">{c.source}</span>}
        <span style={{ color: "var(--ink-3)", fontSize: 12.5 }}>
          open {fmtAgo(c.since_ms)} · seen {c.count}× · {c.needs_human ? "needs you" : "self-healing"}
          {c.notified_ms ? " · pinged" : ""}{snoozed ? " · snoozed" : ""}
        </span>
        <span className="right" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{open ? "hide" : "show"}</span>
      </div>

      {open && (
        <div style={{ marginTop: 10 }}>
          {c.detail && (
            <div style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, color: "var(--ink-2)",
                          whiteSpace: "pre-wrap", marginBottom: 8 }}>{c.detail}</div>
          )}
          {c.meta?.note && <div style={{ marginBottom: 8 }}>{c.meta.note}</div>}
          <ol style={{ margin: "0 0 10px 18px", padding: 0, lineHeight: 1.6 }}>
            {c.steps.map((st, i) => <li key={i}>{st.replace(/^\d+\.\s*/, "")}</li>)}
          </ol>
          {sources.length > 0 && (
            <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginBottom: 8 }}>
              Sources on this account: {sources.join(", ")}
            </div>
          )}
          {c.meta?.proxy && (
            <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginBottom: 8 }}>
              Proxy on this account: <b>{c.meta.proxy}</b>
            </div>
          )}
          {(c.meta?.pending || []).length > 0 && (
            <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginBottom: 8 }}>
              {c.kind === "proxy_broken" ? "Handles that will resolve once the proxy works" : "Waiting for an id"}: {c.meta.pending.join(", ")}
            </div>
          )}

          <div className="cactions" style={{ borderTop: "none", paddingTop: 0, marginTop: 4 }}>
            {c.actions.includes("signin") && (
              pooled
                ? <button className="btn btn-brand btn-sm" onClick={() => onSignin(pooled)}>Sign in @{c.account}</button>
                : <button className="btn btn-brand btn-sm"
                          onClick={() => onAdopt({ platform: "ig", label: c.account, login: c.account })}>
                    Add @{c.account} to the pool, then sign in
                  </button>
            )}
            {c.actions.includes("add_source") && (
              <Link className="btn btn-brand btn-sm" to="/watchlists">Add an Instagram source</Link>
            )}
            {c.actions.includes("reenable_sources") && (
              <button className="btn btn-sm" disabled={busy}
                      onClick={() => run({ action: "reenable_sources" },
                        (r) => r.enabled?.length ? `re-enabled: ${r.enabled.join(", ")}` : "no source was switched off")}>
                Re-enable {sources.length ? `${sources.length} source${sources.length === 1 ? "" : "s"}` : "switched-off sources"}
              </button>
            )}
            {c.actions.includes("set_id") && (
              <>
                <input value={pk} onChange={(e) => setPk(e.target.value)} placeholder="numeric profile_id"
                       style={{ width: 170, padding: "5px 8px", border: "1px solid var(--line)", borderRadius: 6,
                                fontFamily: "ui-monospace, monospace", fontSize: 12.5 }} />
                <button className="btn btn-brand btn-sm" disabled={busy || !/^\d+$/.test(pk.trim())}
                        onClick={() => run({ action: "set_id", platform_id: pk.trim() },
                          (r) => `id ${r.platform_id} saved for ${r.label} — collected on the next pass`)}>
                  Save id
                </button>
              </>
            )}
            {c.actions.includes("retry") && (
              <button className="btn btn-sm" disabled={busy}
                      onClick={() => run({ action: "retry" }, () => "hold cleared — the next pass probes once")}>
                {c.kind === "proxy_broken" ? "Proxy fixed — retry now" : "Retry lookups now"}
              </button>
            )}
            {c.actions.includes("resume") && (
              <button className="btn btn-sm" disabled={busy} onClick={() => run({ action: "resume" }, () => "resumed")}>Resume collection</button>
            )}
            {c.actions.includes("resolve") && (
              <button className="btn btn-sm" disabled={busy} onClick={() => run({ action: "resolve" }, () => "closed")}>Mark fixed</button>
            )}
            {!snoozed && (
              <button className="btn btn-ghost btn-sm" disabled={busy}
                      onClick={() => run({ action: "snooze", hours: 6 }, () => "quiet for 6h")}>Snooze 6h</button>
            )}
          </div>
          {msg && <div style={{ marginTop: 6, fontSize: 12.5 }}>{msg}</div>}
        </div>
      )}
    </div>
  );
}

function FixPanel({ conds, focusId, accounts, onAdopt, onChanged, telegram }) {
  const [signin, setSignin] = useState(null);
  if (!conds) return null;
  const list = conds.conditions || [];
  return (
    <>
      {list.length === 0 && focusId && (
        <div className="banner-ok" style={{ marginBottom: 10 }}>
          <b>Already closed.</b> The condition you were pinged about ({focusId}) is no longer open — it recovered or was fixed.
        </div>
      )}
      {list.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="feed-head" style={{ marginTop: 6 }}>
            <h2>Needs attention</h2>
            <span className="right" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
              {list.filter((c) => c.needs_human).length} need you · {list.filter((c) => !c.needs_human).length} self-healing
              {!telegram && " · Telegram not configured — set TELEGRAM_BOT_TOKEN and ADMIN_TELEGRAM_CHAT_ID in .env"}
            </span>
          </div>
          {list.map((c) => (
            <FixCard key={c.id} c={c} focus={c.id === focusId} accounts={accounts}
                     onAdopt={onAdopt} onChanged={onChanged} onSignin={setSignin} />
          ))}
        </div>
      )}
      {signin && <SignInModal a={signin} onDone={onChanged} onClose={() => setSignin(null)} />}
    </>
  );
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

export default function Accounts({ onMenu }) {
  const pool = useApi(() => api.pool(), [], { every: 30_000 });
  const liveX = useApi(() => api.status(), [], { every: 30_000 });
  const liveIg = useApi(() => api.igStatus(), []);
  const liveFb = useApi(() => api.fbStatus(), [], { every: 30_000 });
  const conds = useApi(() => api.deciderConditions(), [], { every: 30_000 });
  const [adding, setAdding] = useState(null);   // null | {} | {platform,label,login}

  // ?fix=<condition id> is what a Telegram ping links to; ?snooze=6 on the
  // same link quiets it first. The snooze is applied once and the parameter
  // dropped, so a reload does not snooze again.
  const [params, setParams] = useSearchParams();
  const focusId = params.get("fix") || "";
  useEffect(() => {
    const h = parseFloat(params.get("snooze") || "");
    if (!focusId || !(h > 0)) return;
    api.deciderAction({ action: "snooze", id: focusId, hours: h })
      .catch(() => {})
      .finally(() => {
        const next = new URLSearchParams(params);
        next.delete("snooze");
        setParams(next, { replace: true });
        conds.reload();
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);

  const reload = () => { pool.reload(); liveX.reload(); liveIg.reload(); liveFb.reload(); conds.reload(); };

  // Facebook runs ONE burner session (cookies / password / saved state), not a
  // list of accounts — shape it like one live entry so it shows up here too.
  const fbLive = () => {
    const d = liveFb.data;
    if (!d) return [];
    const srcs = d.sources || [];
    const last = srcs.reduce((a, s) => Math.max(a, s.last_run || 0), 0);
    const posts = d.totals?.posts ?? 0;
    return [{
      label: "facebook",
      username: d.session?.identity || "facebook session",
      active: !!d.enabled,
      requests: null,
      proxy: false,
      reasons: d.enabled
        ? [
            `signed in via ${d.session?.method || "saved state"}` +
              (d.session?.state_saved ? " · session state saved on server" : ""),
            `${posts.toLocaleString("en-IN")} posts collected from ${srcs.length} page${srcs.length === 1 ? "" : "s"}`,
            last ? `last page check ${fmtAgo(last * 1000)}` : "no page checked yet",
          ]
        : ["no Facebook login configured — set FB_C_USER/FB_XS or FB_EMAIL/FB_PASSWORD in .env"],
      error: d.error || null,
    }];
  };

  const liveList = (p) =>
    p === "x" ? (liveX.data?.accounts || [])
      : p === "ig" ? (liveIg.data?.accounts || [])
      : p === "fb" ? fbLive() : [];

  // Match a managed account to its live session.
  //
  // Case- and @-insensitive, and it passes the WHOLE live record through rather
  // than plucking two fields: the error, the checkpoint tombstone and last_used
  // are exactly the details that explain a card with no green dot, and dropping
  // them here is what left the panel unable to say why an account was quiet.
  const norm = (v) => String(v || "").trim().toLowerCase().replace(/^@/, "");
  const liveFor = (a) => {
    const label = norm(a.label), login = norm(a.login);
    const rows = liveList(a.platform);
    return rows.find((r) => login && norm(r.username) === login)
      || rows.find((r) => label && norm(r.label) === label)
      || rows.find((r) => label && norm(r.username) === label)
      || null;
  };

  const plats = pool.data?.platforms || {};
  const accounts = pool.data?.accounts || [];

  // Live sessions with no matching pool account = "orphans" to surface + adopt.
  const orphansFor = (p) => {
    const pooled = new Set();
    accounts.filter((a) => a.platform === p).forEach((a) => {
      pooled.add((a.label || "").toLowerCase());
      pooled.add((a.login || "").toLowerCase());
    });
    return liveList(p).filter((r) => {
      const lbl = (r.label || "").toLowerCase(), un = (r.username || "").toLowerCase();
      return !(pooled.has(lbl) || (un && pooled.has(un)));
    });
  };

  return (
    <>
      <PageHead title="Accounts & Sessions" onMenu={onMenu}
                sub="One pool per platform · one active, the rest warm backups · failover on ban">
        <button className="btn btn-brand" onClick={() => setAdding({})}>+ Add account</button>
      </PageHead>

      {pool.loading && liveX.loading && !pool.data && !liveX.data && <Loading />}

      {pool.error && (
        // The pool backend being down must NOT hide the live sessions below —
        // that is exactly how existing accounts "vanished". Warn, don't blank.
        <div className="banner-crit">
          <b>Account pool not reachable.</b> Adding / promoting / failover is unavailable
          ({String(pool.error)}). Your live sessions are still shown below.
        </div>
      )}

      {pool.data && !pool.data.cipher_ready && (
        <div className="banner-crit">
          <b>Set <code>ACCOUNTS_SECRET_KEY</code> in .env.</b> Without it, account passwords and
          2FA secrets can’t be stored — the panel refuses to keep them in plaintext.
        </div>
      )}

      <FixPanel conds={conds.data} focusId={focusId} accounts={accounts}
                telegram={!!conds.data?.telegram}
                onAdopt={(initial) => setAdding(initial)} onChanged={reload} />

      {(() => {
        // One glance across all three platforms before the per-platform detail.
        const actives = accounts.filter((a) => a.status === "active").length;
        const backups = accounts.filter((a) => a.status === "backup").length;
        const attention = accounts.filter(
          (a) => a.status === "needs_login" || a.status === "quarantined"
            || a.status === "dead").length;
        const liveOn = PLATS.reduce(
          (n, [p]) => n + liveList(p).filter((r) => r.active).length, 0);
        return (
          <div className="stats">
            <div className="stat">
              <div className="k">In the pool</div>
              <div className="v">{accounts.length}</div>
              <div className="d">managed accounts, all platforms</div>
            </div>
            <div className="stat">
              <div className="k">Active</div>
              <div className="v">{actives} <small>/ {PLATS.length} platforms</small></div>
              <div className="d">one active per platform is the target</div>
            </div>
            <div className="stat">
              <div className="k">Warm backups</div>
              <div className="v">{backups}</div>
              <div className="d">take over on ban or checkpoint</div>
            </div>
            <div className="stat">
              <div className="k">{attention ? "Needs attention" : "Signed-in sessions"}</div>
              <div className={`v ${attention ? "st-crit" : ""}`}>{attention || liveOn}</div>
              <div className="d">{attention ? "needs login / quarantined / dead" : "live right now"}</div>
            </div>
          </div>
        );
      })()}

      {PLATS.map(([p, title]) => (
        <PlatformSection
          key={p}
          platform={p}
          title={title}
          summary={plats[p] || { active: null, backups: 0, low: false }}
          accounts={accounts.filter((a) => a.platform === p)}
          orphans={orphansFor(p)}
          liveFor={liveFor}
          onAdopt={(initial) => setAdding(initial)}
          onChanged={reload}
        />
      ))}

      {adding !== null && (
        <AddModal initial={adding} onDone={reload} onClose={() => setAdding(null)} />
      )}
    </>
  );
}
