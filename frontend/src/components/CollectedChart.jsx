// "Collected per day" — two series (X, Instagram), one axis, validated
// palette slots 1–2, crosshair + tooltip. Data: /api/metrics per_day.
import React, { useEffect, useRef, useState } from "react";

export default function CollectedChart({ perDay }) {
  const wrapRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [width, setWidth] = useState(320);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth || 320));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const days = perDay?.map((d) => d.day) || [];
  const X = perDay?.map((d) => d.x) || [];
  const IG = perDay?.map((d) => d.ig) || [];
  const n = days.length;
  if (!n) return null;

  const H = 180;
  const m = { t: 12, r: 20, b: 26, l: 44 };
  const iw = Math.max(10, width - m.l - m.r);
  const ih = H - m.t - m.b;
  const rawMax = Math.max(4, ...X, ...IG);
  // A tidy top tick at or above the data max.
  const step = Math.pow(10, Math.floor(Math.log10(rawMax)));
  const ymax = Math.ceil(rawMax / step) * step;
  const xs = (i) => m.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const ys = (v) => m.t + ih - (v / ymax) * ih;
  const path = (a) => a.map((v, i) => `${i ? "L" : "M"}${xs(i).toFixed(1)} ${ys(v).toFixed(1)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(ymax * f));
  const fmtTick = (v) => (v >= 1000 ? `${+(v / 1000).toFixed(1)}k` : v);

  const css = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const c1 = css("--series-1") || "#2a78d6";
  const c2 = css("--series-2") || "#eb6834";

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    let i = Math.round((((e.clientX - r.left) - m.l) / iw) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };

  return (
    <>
      <div className="legend">
        <span><span className="sw" style={{ background: c1 }} />X</span>
        <span><span className="sw" style={{ background: c2 }} />Instagram</span>
      </div>
      <div className="viz-root" ref={wrapRef}>
        <svg width="100%" height={H} role="img"
             aria-label="Posts collected per day, X and Instagram, last 7 days"
             onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          {ticks.map((v) => (
            <g key={v}>
              <line x1={m.l} y1={ys(v)} x2={width - m.r} y2={ys(v)}
                    stroke="var(--grid)" strokeWidth="1" />
              <text x={m.l - 7} y={ys(v) + 3.5} textAnchor="end" fontSize="10"
                    fill="var(--ink-3)" style={{ fontVariantNumeric: "tabular-nums" }}>
                {fmtTick(v)}
              </text>
            </g>
          ))}
          <line x1={m.l} y1={ys(0)} x2={width - m.r} y2={ys(0)}
                stroke="var(--baseline)" strokeWidth="1" />
          {days.map((d, i) =>
            i % 2 ? null : (
              <text key={d + i}
                    x={i === 0 ? m.l : i === n - 1 ? width - 4 : xs(i)}
                    y={H - 8}
                    textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
                    fontSize="10" fill="var(--ink-3)">
                {d}
              </text>
            ),
          )}
          <path d={`${path(X)} L ${xs(n - 1)} ${ys(0)} L ${xs(0)} ${ys(0)} Z`}
                fill={c1} opacity="0.09" />
          <path d={path(X)} fill="none" stroke={c1} strokeWidth="2" strokeLinejoin="round" />
          <path d={path(IG)} fill="none" stroke={c2} strokeWidth="2" strokeLinejoin="round" />
          <text x={xs(n - 1) - 4} y={ys(X[n - 1]) - 8} textAnchor="end"
                fontSize="10.5" fontWeight="600" fill="var(--ink-2)">X</text>
          <text x={xs(n - 1) - 4} y={ys(IG[n - 1]) - 8} textAnchor="end"
                fontSize="10.5" fontWeight="600" fill="var(--ink-2)">IG</text>
          {hover != null && (
            <g>
              <line x1={xs(hover)} x2={xs(hover)} y1={m.t} y2={m.t + ih}
                    stroke="var(--baseline)" strokeWidth="1" strokeDasharray="3 3" />
              <circle cx={xs(hover)} cy={ys(X[hover])} r="4.5" fill={c1}
                      stroke="var(--chart-surface)" strokeWidth="2" />
              <circle cx={xs(hover)} cy={ys(IG[hover])} r="4.5" fill={c2}
                      stroke="var(--chart-surface)" strokeWidth="2" />
            </g>
          )}
        </svg>
        {hover != null && (
          <div className="tip" style={{
            display: "block",
            left: Math.min(Math.max(xs(hover) - 70, 4), width - 148),
            top: m.t - 2,
          }}>
            <div style={{ color: "var(--ink-3)", marginBottom: 3 }}>{days[hover]}</div>
            <div><span className="sw" style={{ background: c1, marginRight: 6 }} />X <b>{X[hover].toLocaleString()}</b></div>
            <div><span className="sw" style={{ background: c2, marginRight: 6 }} />Instagram <b>{IG[hover].toLocaleString()}</b></div>
          </div>
        )}
      </div>
    </>
  );
}
