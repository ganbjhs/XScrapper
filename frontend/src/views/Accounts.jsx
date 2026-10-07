// Account Control Panel — manage the scraper accounts of all three platforms
import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, fmtAgo, useApi } from "../api/client.js";
import { PageHead } from "../App.jsx";
import { Empty, ErrorState, Loading, Modal } from "../components/ui.jsx";
import Diagnosis from "../components/Diagnosis.jsx";

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

// Add / edit / backup-code modals

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

// A NEW PHONE, IN A CHOSEN COUNTRY (ig_identity.MARKETS). The phone must live
// where the account's proxy exits: the picker starts on the exit's country and
// says so when the two disagree. Only THIS account changes — every other
// account keeps the phone in its own device file.
// The country this account's requests leave from NOW. `exit_now` (server:
// ig_session.exit_now) is the latest evidence through the proxy on file; the
// sign-in exit is used only by an older server that does not send it.
function exitCountry(live) {
  if (live?.exit_now) return (live.exit_now.country || "").toUpperCase();
  return (live?.exit?.country || "").toUpperCase();
}

function NewPhoneModal({ a, live, onDone, onClose }) {
  const [markets, setMarkets] = useState(null);
  const [country, setCountry] = useState("");
  const [zone, setZone] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const exitCc = exitCountry(live);
  const exitIp = live?.exit_now ? live.exit_now.ip : live?.exit?.exit_ip;
  const nowCc = (live?.identity?.country || "").toUpperCase();

  useEffect(() => {
    let dead = false;
    api.igMarkets().then((r) => {
      if (dead) return;
      const list = r.markets || [];
      setMarkets(list);
      const has = (cc) => list.some((m) => m.country === cc);
      setCountry(has(exitCc) ? exitCc : has(nowCc) ? nowCc : (r.default || "IN"));
    }).catch((e) => { if (!dead) setErr(String(e.message || e)); });
    return () => { dead = true; };
  }, []);

  const m = (markets || []).find((x) => x.country === country);
  const exitKnown = exitCc && (markets || []).some((x) => x.country === exitCc);
  const go = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api.igReseed(live.username, country, zone);
      if (r && r.error) { setErr(r.error); return; }
      onDone(); onClose();
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`New phone for ${a.label}`} onClose={onClose}
           sub="The current session dies with the old phone. You will sign in again on the new one.">
      <div className="kv"><span>phone now</span>
        <b style={{ fontWeight: 500 }}>{live?.identity?.text || "none minted yet"}</b></div>
      <div className="kv"><span>proxy exits in</span>
        <b style={{ fontWeight: 500 }}>
          {exitCc ? `${exitCc}${exitIp ? ` · ${exitIp}` : ""}`
            : "unknown — press Check proxy on the card first, then choose"}
        </b></div>
      {!markets && !err && <Loading />}
      {markets && (
        <>
          <div className="field"><label>Country of the new phone</label>
            <select value={country} onChange={(e) => { setCountry(e.target.value); setZone(""); }}>
              {markets.map((x) => (
                <option key={x.country} value={x.country}>
                  {x.name} ({x.country}) · {x.phones} phones{x.country === exitCc ? " · matches the proxy" : ""}
                </option>
              ))}
            </select>
          </div>
          {m && m.zones.length > 1 && (
            <div className="field"><label>Time zone</label>
              <select value={zone} onChange={(e) => setZone(e.target.value)}>
                <option value="">Automatic — from the proxy's location</option>
                {m.zones.map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
            </div>
          )}
          {exitCc && country !== exitCc && (
            <div className="err">
              {exitKnown
                ? `This account's proxy exits in ${exitCc}, but you are choosing ${country}. A phone and an IP that disagree about the country is what gets an account challenged.`
                : `This account's proxy exits in ${exitCc}, and there are no phones catalogued for ${exitCc} yet. Any choice here will disagree with the proxy.`}
            </div>
          )}
        </>
      )}
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-brand" disabled={busy || !country} onClick={go}>
          Mint the new phone
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

// Sign in — one path for all three platforms, and it is not a browser

const NEEDS_HINT = {
  proxy: "The residential proxy is missing or its exit is unusable. Fix it on this card (Edit → proxy URL — another session number if the exit is dead), then sign in again.",
  totp: "Add the account's TOTP secret on this card (Edit → TOTP secret), or paste a session instead.",
  paste: "Paste a session from a browser you are already signed into.",
  browser: "Open this account's own browser below (its phone, its proxy), clear what Instagram asks, and the session is adopted for you.",
};

// The streamed browser window — the account's OWN Chromium on the server
function BrowserLoginModal({ a, onDone, onClose }) {
  const [win, setWin] = useState(null);       // {width,height,phone,...}
  const [state, setState] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [hint, setHint] = useState("");     // what Instagram wants from a person, if anything
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
      setHint(r.hint || "");
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
      setHint(r.hint || "");
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
                      width: win ? "auto" : 380 }}>
          {win ? (
            // The whole phone screen stays visible without scrolling the
            // modal: height-bound, width follows the phone's aspect ratio.
            <img ref={imgRef} src={`/api/login/frame?t=${tick}`} alt="the sign-in window"
                 onClick={click} draggable={false}
                 style={{ display: "block", height: "min(72vh, 760px)", width: "auto",
                          cursor: busy ? "progress" : "pointer",
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
          {hint && (
            <div className={state === "challenge" ? "banner-crit" : "banner-warn"} style={{ marginTop: 8, marginBottom: 0, fontSize: 12.5, lineHeight: 1.5 }}>
              {hint}
            </div>
          )}
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
            window answers "Save your login info?" with Save Info for you, then the
            session is adopted by the collector on this same phone and this window closes.
          </div>
          {done && (
            <div className={done.active ? "banner-ok" : "banner-crit"} style={{ marginTop: 10 }}>
              <b>{done.active ? "Signed in and adopted." : "Signed in, but not adopted."}</b>{" "}
              {done.username ? `@${done.username}. ` : ""}{done.detail}
              {done.answered === "save_info" ? " Login info saved on this phone — the next sign-in is a tap, not a password." : ""}
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


// Session state — ALWAYS rendered, on every card
// ONE WORD per row on the card (2026-10-07, the operator's rule: the card
// answers "is it fine?", the detail answers "what is wrong?"). Every word that
// is not fine is a button; it opens AccountDetail, which holds the sentences.
function sessionWord(live) {
  if (!live) return ["never", "warn"];
  if (live.checkpoint_at) return ["checkpoint", "crit"];
  if (/cleared|new phone/i.test(live.error || "")) return ["needs login", "warn"];
  if (live.active) return ["ok", "good"];
  return ["not working", "crit"];
}

function Word({ text, tone, onClick }) {
  if (tone === "good" || !onClick) return <b className={tone ? `st-${tone}` : ""}>{text}</b>;
  return (
    <button className={`chip as-btn ${tone}`} onClick={onClick} title="Show what is wrong">
      {text}<span className="chev">›</span>
    </button>
  );
}

// Everything the card no longer says: the sentences, the addresses, the last
// sign-in attempts with what Instagram answered, and this account's own log.
function AccountDetail({ a, live, problems, onClose }) {
  const isIg = a.platform === "ig";
  const names = [live?.username || a.login, a.label].filter(Boolean).join(",");
  const diag = useApi(() => (isIg ? api.igDiagnosis() : Promise.resolve(null)), [a.account_id]);
  const logs = useApi(() => api.activityLogs({ account: names, limit: 80 }), [names]);
  const tries = diag.data?.accounts?.[(live?.username || a.login || "").toLowerCase()] || [];
  const shared = (diag.data?.findings || []).filter(
    (f) => (f.accounts || []).includes((live?.username || a.login || "").toLowerCase()));
  const en = live?.exit_now;
  const H = ({ children }) => (
    <div style={{ fontSize: 11, letterSpacing: ".06em", textTransform: "uppercase",
                  color: "var(--ink-3)", margin: "14px 0 4px" }}>{children}</div>
  );
  return (
    <Modal title={a.label} sub={`@${a.login} · ${a.platform.toUpperCase()}`} onClose={onClose} wide>
      <H>What is wrong</H>
      {problems.length === 0 && <div className="kv"><span>status</span><b className="st-good">nothing</b></div>}
      {problems.map((p, i) => (
        <div className="kv" key={i}><span>{p.label}</span><b className={`st-${p.tone || "warn"}`}>{p.text}</b></div>
      ))}
      {shared.map((f, i) => (
        <div className="kv" key={`s${i}`}><span style={{ whiteSpace: "nowrap" }}>{f.level === "measured" ? "measured" : "shared"}</span>
          <b style={{ fontWeight: 500 }}>{f.title}</b></div>
      ))}

      {isIg && live && (
        <>
          <H>Phone and address</H>
          <div className="kv"><span>phone</span><b style={{ fontWeight: 500 }}>{live.identity?.text || "none yet"}</b></div>
          <div className="kv"><span>fetching via</span><b style={{ fontWeight: 500 }}>
            {en?.ip ? `${en.ip}${en.country ? ` (${en.country})` : ""}${en.at ? ` · checked ${fmtAgo(en.at)}` : ""}`
              : en?.proxy_host ? `proxy ${en.proxy_host} — not checked since it changed` : "no proxy on file"}</b></div>
          {live.exit?.exit_ip && (
            <div className="kv"><span>signed in via</span><b style={{ fontWeight: 400, color: "var(--ink-3)" }}>
              {`${live.exit.exit_ip}${live.exit.country ? ` (${live.exit.country})` : ""}`}
              {live.exit.checked ? ` · ${String(live.exit.checked).slice(0, 10)}` : ""}
              {en?.ip && en.ip !== live.exit.exit_ip ? " · a previous proxy" : ""}</b></div>
          )}
        </>
      )}

      {isIg && (
        <>
          <H>Last sign-in attempts</H>
          {tries.length === 0 && <div className="kv"><span>none recorded yet</span><b /></div>}
          {tries.map((t, i) => (
            <div className="kv" key={i}>
              <span style={{ whiteSpace: "nowrap" }}>{t.door}{t.stage && t.stage !== "login" ? ` (${t.stage})` : ""} · {fmtAgo(new Date(t.ts_ms).toISOString())}</span>
              <b className={t.ok ? "st-good" : "st-crit"} style={{ fontWeight: 500 }}>
                {t.ok ? "ok" : [t.http_status ? `HTTP ${t.http_status}` : t.why,
                                t.answered_by && `by ${t.answered_by}`,
                                t.exit_ip && `via ${t.exit_ip}${t.country ? ` (${t.country})` : ""}`,
                                t.browser, t.detail].filter(Boolean).join(" · ")}
              </b>
            </div>
          ))}
        </>
      )}

      <H>This account's log</H>
      <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid var(--ring)", borderRadius: 8,
                    padding: "6px 10px", fontSize: 12, lineHeight: 1.65, fontFamily: "var(--mono, ui-monospace, monospace)" }}>
        {logs.loading && "…"}
        {!logs.loading && (logs.data?.events || []).length === 0 && "no lines for this account"}
        {(logs.data?.events || []).map((e) => (
          <div key={e.id} className={e.level === "error" ? "st-crit" : e.level === "warn" ? "st-warn" : e.level === "ok" ? "st-good" : ""}>
            <span style={{ color: "var(--ink-3)" }}>
              {new Date(e.ts_ms).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
            </span>{"  "}{String(e.message).trim()}
          </div>
        ))}
      </div>
      <div className="row"><button className="btn" onClick={onClose}>Close</button></div>
    </Modal>
  );
}


// A managed (pool) account card — full controls

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
  const isIg = a.platform === "ig";
  const bench = () => act(() => api.igAccount(live.username, false), "benched — its sources move on the next pass");
  const collect = () => act(() => api.igAccount(live.username, true), "collecting again from the next pass");
  const newPhone = () => setModal("phone");
  const clearSession = () => {
    if (!confirm(`Remove the saved session for ${a.label}? Its cookies and its browser profile on the server are deleted, so the next sign-in starts clean. The phone and the proxy are kept. The account stops collecting until you sign in again.`)) return;
    act(() => api.igSessionClear(live.username), "session removed — sign in again");
  };
  const [checking, setChecking] = useState(false);
  const checkProxy = async () => {
    setChecking(true); setMsg("checking where the proxy exits…");
    try {
      const r = await api.igExitCheck(live.username);
      const e = r.exit_now || {};
      setMsg(`proxy exits at ${e.ip || "?"}${e.country ? ` (${e.country})` : ""}`
             + (r.usable ? "" : ` — NOT usable: ${r.detail}`) + (r.warn ? ` — ${r.warn}` : ""));
      onChanged();
    } catch (e) { setMsg(String(e.message || e)); } finally { setChecking(false); }
  };
  // The phone and the proxy must agree about the country (ig_identity.MARKETS).
  const phoneCc = (live?.identity?.country || "").toUpperCase();
  const exitCc = exitCountry(live);
  const en = live?.exit_now;
  const countryClash = !!(phoneCc && exitCc && phoneCc !== exitCc);
  const [sw, st] = sessionWord(live);
  const openDetail = () => setModal("detail");
  // Every reason this card is not fine, as sentences — shown in the detail only.
  const problems = [];
  if (!live) problems.push({ label: "session", text: "never signed in on this server" });
  else if (live.checkpoint_at) problems.push({ label: "session", tone: "crit",
    text: `checkpoint — ${a.platform === "ig" ? "Instagram" : "the platform"} wants a human` });
  else if (!live.active && !live.error) problems.push({ label: "session", tone: "crit", text: "signed in once, not working now" });
  if (live?.error) problems.push({ label: "session error", tone: "crit", text: live.error });
  if (a.health && a.health !== live?.error) problems.push({ label: "health", tone: "crit", text: a.health });
  if (!a.has_proxy) problems.push({ label: "proxy", tone: "crit", text: "no proxy — requests would leave from the server's own address" });
  if (countryClash) problems.push({ label: "country",
    text: `phone is in ${live.identity.country_name || phoneCc}, proxy exits in ${exitCc} — New phone to match, or move the proxy` });
  if (live?.identity?.legacy) problems.push({ label: "phone", text: "legacy default phone — the next sign-in mints a real one" });

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
          <button className="btn btn-ghost btn-sm" onClick={openDetail}>Details</button>
        </span>
      </div>

      <div className="kv"><span>login</span><b>{a.login}</b></div>
      <div className="kv"><span>session</span><Word text={sw} tone={st} onClick={openDetail} /></div>
      {isIg && live && (
        <div className="kv"><span>collecting</span>
          <b className={live.active && !live.checkpoint_at ? "st-good" : ""}>
            {live.active ? `yes · ${live.owns ?? 0}` : "benched"}</b></div>
      )}
      <div className="kv"><span>proxy</span>
        {!a.has_proxy ? <Word text="none" tone="crit" onClick={openDetail} />
          : countryClash ? <Word text="mismatch" tone="warn" onClick={openDetail} />
          : <b>{isIg && en?.country ? en.country : (a.proxy_id || "set")}</b>}
      </div>
      <div className="kv"><span>health</span>
        <Word text={problems.length ? "not ok" : "ok"} tone={problems.length ? "crit" : "good"} onClick={openDetail} /></div>
      <div className="kv"><span>last success</span>
        <b>{a.last_success_at ? fmtAgo(a.last_success_at) : "never"}</b></div>
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
        {isIg && live && <button disabled={checking} onClick={checkProxy}>Check proxy</button>}
        {isIg && live && <button onClick={clearSession}>Clear session</button>}
        {a.status !== "quarantined" && a.status !== "dead"
          ? <button onClick={quarantine}>Quarantine</button>
          : <button onClick={revive}>Return to pool</button>}
        <button onClick={remove} style={{ color: "var(--critical)" }}>Remove</button>
      </div>

      {modal === "detail" && <AccountDetail a={a} live={live} problems={problems} onClose={() => setModal(null)} />}
      {modal === "signin" && <SignInModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
      {modal === "edit" && <EditModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
      {modal === "codes" && <CodesModal a={a} onDone={onChanged} onClose={() => setModal(null)} />}
      {modal === "phone" && <NewPhoneModal a={a} live={live} onDone={onChanged} onClose={() => setModal(null)} />}
    </div>
  );
}

// A live session that isn't in the pool yet — read-only + "Add to pool"

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

// One platform section: pool accounts, then live-not-in-pool sessions

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

      {/* A thin pool is worth knowing and is not an incident. It was a
          banner-crit (a red bar, full width, every page load) for a platform
          that might not even be in use this week — which is most of what made
          this page feel like it was shouting. One muted line, in place. */}
      {summary.low && accounts.length > 0 && (
        <div style={{ color: "var(--ink-3)", fontSize: 12.5, margin: "-2px 0 10px", lineHeight: 1.5 }}>
          Pool is thin — {summary.backups} warm backup{summary.backups === 1 ? "" : "s"} for {title}.
          Add another so a ban never causes an outage.
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

// The Fix panel — what the decider (decider.py) needs a human for.

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
              {["proxy_broken", "proxy_auth"].includes(c.kind) ? "Handles that will resolve once the proxy works" : "Waiting for an id"}: {c.meta.pending.join(", ")}
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
                {["proxy_broken", "proxy_auth"].includes(c.kind) ? "Proxy fixed — retry now" : "Retry lookups now"}
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

// "Needs attention" means A HUMAN IS NEEDED — nothing else (2026-09-12).
function FixPanel({ conds, focusId, accounts, onAdopt, onChanged, telegram }) {
  const [signin, setSignin] = useState(null);
  const [showSelf, setShowSelf] = useState(false);
  if (!conds) return null;
  const list = conds.conditions || [];
  // A focused (linked-to) condition is always shown, whatever its kind.
  const mine = list.filter((c) => c.needs_human || c.id === focusId);
  const selfHealing = list.filter((c) => !(c.needs_human || c.id === focusId));
  return (
    <>
      {list.length === 0 && focusId && (
        <div className="banner-ok" style={{ marginBottom: 10 }}>
          <b>Already closed.</b> The condition you were pinged about ({focusId}) is no longer open — it recovered or was fixed.
        </div>
      )}
      {mine.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="feed-head" style={{ marginTop: 6 }}>
            <h2>Needs attention</h2>
            <span className="right" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
              {mine.length} waiting on you
              {!telegram && " · nobody is paged — set the admin bot in Settings"}
            </span>
          </div>
          {mine.map((c) => (
            <FixCard key={c.id} c={c} focus={c.id === focusId} accounts={accounts}
                     onAdopt={onAdopt} onChanged={onChanged} onSignin={setSignin} />
          ))}
        </div>
      )}
      {selfHealing.length > 0 && (
        <div style={{ marginBottom: 12, fontSize: 12.5, color: "var(--ink-3)", lineHeight: 1.6 }}>
          {selfHealing.length === 1
            ? "1 condition is clearing itself"
            : `${selfHealing.length} conditions are clearing themselves`}{" ("}
          {selfHealing.map((c) => c.kind).filter((k, i, a) => a.indexOf(k) === i).join(", ")}
          {") — the collector is backing off and nothing is needed from you. "}
          <button className="btn btn-ghost btn-sm" onClick={() => setShowSelf((v) => !v)}>
            {showSelf ? "Hide" : "Show anyway"}
          </button>
          {showSelf && (
            <div style={{ marginTop: 8 }}>
              {selfHealing.map((c) => (
                <FixCard key={c.id} c={c} focus={false} accounts={accounts}
                         onAdopt={onAdopt} onChanged={onChanged} onSignin={setSignin} />
              ))}
            </div>
          )}
        </div>
      )}
      {signin && <SignInModal a={signin} onDone={onChanged} onClose={() => setSignin(null)} />}
    </>
  );
}

// The view

export default function Accounts({ onMenu }) {
  const pool = useApi(() => api.pool(), [], { every: 30_000 });
  const liveX = useApi(() => api.status(), [], { every: 30_000 });
  // Same 30 s refresh as the others. Loaded once, the Instagram session rows
  // Not api.igStatus(): with no project it answers 200 + "no project
  // selected", which request() threw away along with the accounts — so every
  // Instagram card read "never signed in" and lost its phone row, Bench and
  // New phone (since the project scoping, 77442c8; found 2026-10-03).
  const liveIg = useApi(() => api.igAccountsLive(), [], { every: 30_000 });
  const liveFb = useApi(() => api.fbStatus(), [], { every: 30_000 });
  const conds = useApi(() => api.deciderConditions(), [], { every: 30_000 });
  const [adding, setAdding] = useState(null);   // null | {} | {platform,label,login}

  // ?fix=<condition id> is what a Telegram ping links to; ?snooze=6 on the same link quiets it
  // first.
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

      <Diagnosis accounts={accounts} />

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
            {/* One tile, one meaning. It used to swap its own label and go red
                whenever ANY account was needs_login / quarantined / dead — so a
                burner retired weeks ago kept the page looking alarmed, and the
                number under "Signed-in sessions" was sometimes not sessions at
                all. Sessions is the number you read daily; anything waiting on
                a human is said underneath, in words, only when it is true. */}
            <div className="stat">
              <div className="k">Signed-in sessions</div>
              <div className="v">{liveOn}</div>
              <div className={`d ${attention ? "st-warn" : ""}`}>
                {attention
                  ? `live right now · ${attention} account${attention === 1 ? "" : "s"} need a login`
                  : "live right now"}
              </div>
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
