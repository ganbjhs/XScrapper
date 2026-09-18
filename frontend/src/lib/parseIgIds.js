// Parse a pasted handle -> Instagram profile_id list.
//
// WHY THIS EXISTS: a source the collector has never resolved to a numeric id
// cannot be read at all (store_ig: platform_id is the thing Instagram's media
// endpoint accepts; the handle is only the thing a human reads). When the name
// lookup endpoint is throttled — which, per the 2026-09-17 finding, it is for
// every collecting account at once — the ONLY way forward is a human pasting
// ids in by hand. So this parser's job is to accept whatever shape that human
// already has, not to teach them a format:
//
//   {"natgeo": "787132", "nasa": 528817151}          JSON object
//   [{"handle": "natgeo", "profile_id": "787132"}]   JSON array of records
//   [["natgeo", "787132"]]                           JSON array of pairs
//   natgeo 787132                                    whitespace
//   natgeo,787132   natgeo:787132   natgeo | 787132  csv / colon / pipe
//   @natgeo - 787132                                 dash, leading @
//   https://www.instagram.com/natgeo/  787132        pasted profile URL
//   787132 natgeo                                    reversed
//
// It never throws and never guesses an id: a line it cannot read with
// certainty comes back in `bad` so the panel can show it to the operator
// rather than silently dropping it.

const HANDLE_KEYS = ["handle", "username", "user_name", "user", "name",
                     "label", "account", "profile"];
const ID_KEYS = ["profile_id", "platform_id", "user_id", "userid", "pk",
                 "id", "ig_id"];

// Instagram usernames are letters, digits, periods and underscores.
const HANDLE_RE = /^[A-Za-z0-9._]+$/;

export const cleanHandle = (raw) => {
  let h = String(raw ?? "").trim();
  if (!h) return "";
  const m = h.match(/instagram\.com\/+([A-Za-z0-9._]+)/i);
  if (m) h = m[1];
  h = h.replace(/^@+/, "").replace(/\/+$/, "").trim();
  return HANDLE_RE.test(h) ? h : "";
};

// An id is numeric or it is not an id. A "handle" in the id column is the
// commonest paste mistake and it must NOT be written to platform_id — the
// collector would then fetch by a string Instagram rejects, and the row would
// look resolved while collecting nothing.
export const cleanId = (raw) => {
  const s = String(raw ?? "").trim().replace(/^["']|["']$/g, "");
  return /^\d+$/.test(s) ? s : "";
};

const pick = (obj, keys) => {
  const lower = {};
  for (const k of Object.keys(obj)) lower[k.toLowerCase().replace(/[\s-]/g, "_")] = obj[k];
  for (const k of keys) if (lower[k] !== undefined && lower[k] !== null) return lower[k];
  return undefined;
};

const splitLine = (line) => {
  let rest = line, urlHandle = "";
  // Pull a profile URL out FIRST — its own colons and slashes would otherwise
  // be read as field separators and shatter it.
  const url = rest.match(/https?:\/\/[^\s,;|]*instagram\.com\/+([A-Za-z0-9._]+)\/*/i);
  if (url) {
    urlHandle = url[1];
    rest = rest.slice(0, url.index) + " " + rest.slice(url.index + url[0].length);
  }
  const toks = rest
    .split(/[\s,;:|=\t]+|\s+-+\s+/)
    .map((t) => t.replace(/^["'[{]+|["'\]},]+$/g, "").trim())
    .filter(Boolean);
  return { urlHandle, toks };
};

export function parseIgIds(text) {
  const src = String(text ?? "").trim();
  const pairs = [];
  const bad = [];
  if (!src) return { pairs, bad };

  const push = (h, i, raw) => {
    const H = cleanHandle(h), I = cleanId(i);
    if (H && I) pairs.push({ handle: H, id: I });
    else bad.push(String(raw ?? `${h} ${i}`).trim());
  };

  // ---- JSON -------------------------------------------------------------
  let json = null;
  if (/^[[{]/.test(src)) { try { json = JSON.parse(src); } catch { json = null; } }

  if (json && typeof json === "object" && !Array.isArray(json)) {
    for (const [k, v] of Object.entries(json)) {
      if (v && typeof v === "object" && !Array.isArray(v)) {
        push(pick(v, HANDLE_KEYS) ?? k, pick(v, ID_KEYS), `${k}: ${JSON.stringify(v)}`);
      } else {
        push(k, v, `${k}: ${v}`);
      }
    }
    return { pairs, bad };
  }
  if (Array.isArray(json)) {
    for (const el of json) {
      if (Array.isArray(el)) push(el[0], el[1], el.join(" "));
      else if (el && typeof el === "object") {
        push(pick(el, HANDLE_KEYS), pick(el, ID_KEYS), JSON.stringify(el));
      } else bad.push(String(el));
    }
    return { pairs, bad };
  }

  // ---- line based -------------------------------------------------------
  for (const rawLine of src.split(/\r?\n/)) {
    const line = rawLine.replace(/^\s*[-*•]\s+/, "").trim().replace(/[,;]\s*$/, "");
    if (!line || /^(#|\/\/)/.test(line)) continue;
    const { urlHandle, toks } = splitLine(line);

    if (urlHandle) { push(urlHandle, toks.find((t) => cleanId(t)), line); continue; }
    if (toks.length < 2) { bad.push(line); continue; }

    const first = toks[0], last = toks[toks.length - 1];
    // Whichever end is numeric is the id — this is what makes both
    // "natgeo 787132" and "787132 natgeo" work without asking the operator
    // which column order their export used.
    if (cleanId(last)) push(first, last, line);
    else if (cleanId(first)) push(last, first, line);
    else bad.push(line);
  }
  return { pairs, bad };
}

export default parseIgIds;
