// The sentiment strip — every category and how many posts carry it, sitting at
// the top of the page rather than buried one click into a board.
//
// It also owns the two buttons that act on the whole project: Classify, which
// now covers every unlabelled post rather than a slice, and Export, which is
// the only export the Collections page offers. Both belong beside the counts
// they change; a button that fills these tiles and lives on another screen is a
// button nobody presses.
//
// Classifying is a background job now, so this polls /api/labels/status while a
// run is going and shows a bar that moves. The old shape held the request open
// and then apologised for work that was, in fact, still running.
import React, { useEffect, useRef, useState } from "react";
import { api, fmtN } from "../api/client.js";

// Poll while a run is in flight, stop the moment it is not. Deliberately not
// `useApi({ every })`: a dashboard that polls a labelling endpoint forever
// costs a query a second on a page nobody is looking at.
export function useLabelRun(labels, onFinished) {
  const run = labels.data?.run || null;
  const running = !!run?.running;
  const was = useRef(false);
  const reload = labels.reload;

  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => reload(true), 1500);
    return () => clearInterval(t);
  }, [running, reload]);

  useEffect(() => {
    if (was.current && !running && onFinished) onFinished(run);
    was.current = running;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  return run;
}

// What a finished run should say in one line. "Nothing happened" is the one
// answer this may not give, so every stop reason is named.
function runSummary(run) {
  if (!run) return "";
  const n = fmtN(run.done || 0);
  if (run.stop_reason === "empty") {
    return "✓ Nothing left — every post already carries a label.";
  }
  if (run.stop_reason === "error") {
    return `✗ Stopped after ${n}: ${run.error || "the model refused"}`;
  }
  const cost = `$${Number(run.cost_usd || 0).toFixed(4)}`;
  if (run.stop_reason === "partial") {
    return `⚠ ${n} labelled, ${fmtN(run.failed || 0)} not (${cost}) — `
      + `${run.error || "some batches failed"}. Press Classify again to retry them.`;
  }
  return `✓ ${n} labelled${run.failed ? `, ${fmtN(run.failed)} not` : ""} (${cost}).`;
}

// A hook, not a component: it owns state (is a start in flight? what did the
// last one say?) but its caller decides where the button and the message go —
// the Live Feed puts them in its header, the strip below puts them in a panel.
export function useClassifyButton({ pid, labels, big = false }) {
  const [starting, setStarting] = useState(false);
  const [msg, setMsg] = useState("");
  const run = labels.data?.run || null;
  const running = starting || !!run?.running;
  // One run goes at a time across the whole server. If it belongs to another
  // project, say so rather than offering a button that would be refused.
  const elsewhere = !!run?.busy_elsewhere;
  const waiting = labels.data?.unlabelled || 0;
  const key = labels.data?.key_present;

  const start = async () => {
    if (!pid || running) return;
    setStarting(true);
    setMsg("");
    try {
      const r = await api.classify(pid);
      if (r?.error) setMsg(`✗ ${r.error}`);
      else setMsg("");
      labels.reload(true);
    } catch (e) {
      setMsg(`✗ ${String(e.message || e)}`);
    } finally {
      // The server flips `running` on before it answers, so the poll takes
      // over from here.
      setStarting(false);
    }
  };

  const label = () => {
    if (running) {
      const total = run?.total || 0;
      if (!total) return "Starting…";
      return `Classifying ${fmtN(run.done || 0)} / ${fmtN(total)}`;
    }
    if (!labels.data) return "Classify";
    if (!key) return "Classify — no key";
    if (elsewhere) return "Busy — another project";
    if (!waiting) return "All classified";
    return `Classify all ${fmtN(waiting)}`;
  };

  return {
    msg,
    setMsg,
    node: (
      <button className={`btn ${big ? "btn-brand" : "btn-ghost"}`}
              disabled={running || elsewhere || !key || !waiting}
              title={!key
                ? "No Grok key on the server — add XAI_API_KEY to .env"
                : elsewhere
                  ? "Another project is classifying — one run at a time, so "
                    + "two runs cannot pay twice for the same posts"
                  : !waiting
                    ? "Every post in this project already has a label"
                    : `Send all ${fmtN(waiting)} unlabelled posts to `
                      + `${labels.data?.model} — it runs in the background and `
                      + `the counts fill in as they land`}
              onClick={start}>
        {label()}
      </button>
    ),
  };
}

export default function SentimentStrip({ pid, projectName, labels, onOpen }) {
  const [done, setDone] = useState("");
  const run = useLabelRun(labels, (r) => setDone(runSummary(r)));
  const btn = useClassifyButton({ pid, labels, big: true });

  useEffect(() => {
    if (!done) return undefined;
    const t = setTimeout(() => setDone(""), 12000);
    return () => clearTimeout(t);
  }, [done]);

  const d = labels.data;
  const cats = d?.categories || [];
  const counts = d?.counts || {};
  const waiting = d?.unlabelled || 0;
  const running = !!run?.running;
  const pct = running && run.total
    ? Math.min(100, ((run.done + run.failed) / run.total) * 100) : 0;

  const exportHref =
    `/api/collections/export?project=${pid}`
    + `&name=${encodeURIComponent(projectName || "collections")}`;

  return (
    <div className="panel sent-strip">
      <div className="phead">
        <h3>Sentiments</h3>
        <span className="right sent-actions">
          {btn.node}
          <a className="btn btn-ghost" href={exportHref}
             title="One Excel file: a summary sheet, then every category and its posts">
            ⤓ Export Excel
          </a>
        </span>
      </div>

      <div className="sent-tiles">
        {cats.map((c) => (
          <button key={c.key} className={`sent-tile cat-${c.key}`}
                  onClick={() => onOpen && onOpen(c)}
                  title={onOpen ? `Open the ${c.name} board` : c.name}>
            <b>{fmtN(counts[c.key] || 0)}</b>
            <span>{c.name}</span>
          </button>
        ))}
        <div className="sent-tile waiting" title="Posts with no label yet">
          <b>{fmtN(waiting)}</b>
          <span>Not classified</span>
        </div>
      </div>

      {running && (
        <>
          <div className="meter" style={{ marginTop: 12 }}>
            <span style={{ width: `${pct}%` }} />
          </div>
          <div className="sub" style={{ marginTop: 6 }}>
            {fmtN(run.done || 0)} of {fmtN(run.total || 0)} labelled
            {run.failed ? `, ${fmtN(run.failed)} not` : ""}
            {" · "}${Number(run.cost_usd || 0).toFixed(4)} so far
            {" · "}it keeps going if you leave this page.
          </div>
        </>
      )}

      {!running && (btn.msg || done) && (
        <div className={`sub ${(btn.msg || done).startsWith("✗") ? "st-crit"
          : (btn.msg || done).startsWith("⚠") ? "st-warn" : "st-good"}`}
             style={{ marginTop: 10, fontWeight: 600 }}>
          {btn.msg || done}
        </div>
      )}

      {d && !d.key_present && (
        <div className="banner-crit" style={{ marginTop: 10 }}>
          No Grok key on the server. Add <code>XAI_API_KEY</code> to
          <code> .env</code> and restart — the key is never stored or edited here.
        </div>
      )}
    </div>
  );
}
