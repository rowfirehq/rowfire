import { useLayoutEffect, useMemo, useRef, useState } from "react";

import type { SeriesPoint } from "../api";

/* Single-series column chart: magnitude over time.
 *
 * Form choice: one series, so no legend (the title names it) and no
 * categorical palette — the series hue is slot 1, validated against both
 * surfaces.
 *
 * The load-bearing detail is that a zero day renders **no mark at all**,
 * while any non-zero day gets at least 2px. A quiet day and an empty day must
 * not look alike: one means the trigger fires rarely, the other means it did
 * not fire. The fixture's 29-day hole is the case this exists for.
 */

const PAD = { top: 18, right: 12, bottom: 26, left: 46 };
const PLOT_H = 190;
const BAR_GAP = 2; // surface gap between adjacent bars
const MIN_BAR = 2; // a count of 1 must still be visible
const CAP_RADIUS = 4; // rounded data-end, square at the baseline

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = step * magnitude;
    if (candidate >= value) return candidate;
  }
  return 10 * magnitude;
}

/** Sum adjacent days until each column is wide enough to see. */
function bucketSeries(points: SeriesPoint[], maxColumns: number) {
  if (points.length <= maxColumns) {
    return { columns: points.map((p) => ({ ...p, days: 1, end: p.date })), perColumn: 1 };
  }
  const step = points.length / maxColumns;
  const columns: { date: string; end: string; count: number; days: number }[] = [];
  for (let i = 0; i < maxColumns; i += 1) {
    const start = Math.floor(i * step);
    const end = i === maxColumns - 1 ? points.length : Math.floor((i + 1) * step);
    const slice = points.slice(start, Math.max(end, start + 1));
    columns.push({
      date: slice[0].date,
      end: slice[slice.length - 1].date,
      count: slice.reduce((total, p) => total + p.count, 0),
      days: slice.length,
    });
  }
  return { columns, perColumn: points.length / maxColumns };
}

function barPath(x: number, y: number, width: number, height: number): string {
  const r = Math.min(CAP_RADIUS, width / 2, height);
  const bottom = y + height;
  return [
    `M${x},${bottom}`,
    `L${x},${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `L${x + width - r},${y}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `L${x + width},${bottom}`,
    "Z",
  ].join(" ");
}

const fmtDay = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });

export function Chart({ series, label }: { series: SeriesPoint[]; label: string }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(880);
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);

  useLayoutEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(entry.contentRect.width, 320));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const plotW = Math.max(width - PAD.left - PAD.right, 80);
  // Keep each column at least 3px wide; past that, aggregate rather than
  // rendering sub-pixel slivers that all look like zero.
  const maxColumns = Math.max(Math.floor(plotW / 3), 12);
  const { columns, perColumn } = useMemo(
    () => bucketSeries(series, maxColumns),
    [series, maxColumns],
  );

  const peak = useMemo(
    () => columns.reduce((max, c) => Math.max(max, c.count), 0),
    [columns],
  );
  const top = niceMax(peak);
  const slot = plotW / Math.max(columns.length, 1);
  const barW = Math.max(slot - BAR_GAP, 1);

  const busiestIndex = useMemo(() => {
    let best = -1;
    columns.forEach((c, i) => {
      if (c.count > 0 && (best === -1 || c.count > columns[best].count)) best = i;
    });
    return best;
  }, [columns]);

  const ticks = [0, top / 2, top];
  const totalH = PAD.top + PLOT_H + PAD.bottom;
  const unit = perColumn <= 1.5 ? "day" : `${Math.round(perColumn)} days`;

  const active = hover === null ? null : columns[hover];

  function onMove(event: React.MouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left - PAD.left;
    if (x < 0 || x > plotW) return setHover(null);
    setHover(Math.min(columns.length - 1, Math.max(0, Math.floor(x / slot))));
  }

  return (
    <div>
      <div className="chart-head">
        <span className="chart-title">
          {label} per {unit}
        </span>
        <button className="linkish" onClick={() => setShowTable((v) => !v)}>
          {showTable ? "show chart" : "show table"}
        </button>
      </div>

      {showTable ? (
        <div className="table-scroll scroll-y">
          <table>
            <thead>
              <tr>
                <th>date</th>
                <th>fires</th>
              </tr>
            </thead>
            <tbody>
              {series.map((point) => (
                <tr key={point.date}>
                  <td className="num">{point.date}</td>
                  <td className="num">{point.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart-wrap" ref={wrapRef}>
          <svg
            width={width}
            height={totalH}
            role="img"
            aria-label={`${label} per ${unit}. Peak ${peak}. ${
              columns.filter((c) => c.count === 0).length
            } of ${columns.length} columns are empty.`}
            onMouseMove={onMove}
            onMouseLeave={() => setHover(null)}
            style={{ display: "block" }}
          >
            {/* Gridlines: solid hairlines, one step off the surface. */}
            {ticks.map((tick) => {
              const y = PAD.top + PLOT_H - (tick / top) * PLOT_H;
              return (
                <g key={tick}>
                  <line
                    x1={PAD.left}
                    x2={PAD.left + plotW}
                    y1={y}
                    y2={y}
                    stroke={tick === 0 ? "var(--baseline)" : "var(--gridline)"}
                    strokeWidth="1"
                  />
                  <text
                    x={PAD.left - 8}
                    y={y + 4}
                    textAnchor="end"
                    fontSize="11"
                    fill="var(--text-muted)"
                    style={{ fontVariantNumeric: "tabular-nums" }}
                  >
                    {Math.round(tick).toLocaleString()}
                  </text>
                </g>
              );
            })}

            {/* Hover band, drawn under the bars so it never hides one. */}
            {hover !== null && (
              <rect
                x={PAD.left + hover * slot - BAR_GAP / 2}
                y={PAD.top}
                width={slot}
                height={PLOT_H}
                fill="var(--series-1-wash)"
              />
            )}

            {columns.map((column, index) => {
              // A zero column renders nothing at all -- this is the point.
              if (column.count === 0) return null;
              const h = Math.max((column.count / top) * PLOT_H, MIN_BAR);
              const x = PAD.left + index * slot + BAR_GAP / 2;
              const y = PAD.top + PLOT_H - h;
              return (
                <path
                  key={column.date}
                  d={barPath(x, y, barW, h)}
                  fill="var(--series-1)"
                  opacity={hover === null || hover === index ? 1 : 0.55}
                />
              );
            })}

            {/* Direct-label the extreme only -- never every column. */}
            {busiestIndex >= 0 &&
              hover === null &&
              (() => {
                const column = columns[busiestIndex];
                const h = Math.max((column.count / top) * PLOT_H, MIN_BAR);
                const cx = PAD.left + busiestIndex * slot + slot / 2;
                return (
                  <text
                    x={Math.min(Math.max(cx, PAD.left + 10), PAD.left + plotW - 10)}
                    y={PAD.top + PLOT_H - h - 6}
                    textAnchor="middle"
                    fontSize="11"
                    fontWeight="600"
                    fill="var(--text-primary)"
                  >
                    {column.count}
                  </text>
                );
              })()}

            {columns.length > 0 && (
              <>
                <text
                  x={PAD.left}
                  y={totalH - 8}
                  fontSize="11"
                  fill="var(--text-muted)"
                >
                  {fmtDay(columns[0].date)}
                </text>
                <text
                  x={PAD.left + plotW}
                  y={totalH - 8}
                  textAnchor="end"
                  fontSize="11"
                  fill="var(--text-muted)"
                >
                  {fmtDay(columns[columns.length - 1].end)}
                </text>
              </>
            )}
          </svg>

          {active && (
            <div
              className="tooltip"
              style={{
                left: Math.min(
                  Math.max(PAD.left + hover! * slot + slot / 2 - 60, 0),
                  Math.max(width - 130, 0),
                ),
                top: 0,
              }}
            >
              <div className="t-date">
                {active.days > 1
                  ? `${fmtDay(active.date)} – ${fmtDay(active.end)}`
                  : fmtDay(active.date)}
              </div>
              <div>
                <strong>{active.count.toLocaleString()}</strong> fire
                {active.count === 1 ? "" : "s"}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
