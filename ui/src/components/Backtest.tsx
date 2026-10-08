import { useState } from "react";

import { ApiError, api, formatCell, type BacktestResult } from "../api";
import { Chart } from "./Chart";
import { Stat } from "./Stat";

/* Backtest, inline in the composer.
 *
 * Sitting here rather than on its own screen is the point: a rule and the
 * evidence for it belong together. Nothing is sent -- this only asks what
 * would have happened.
 *
 * Of a *rule*, not a trigger: the trigger says which rows matched, and the
 * rule decides how many of those survive its cadence. The second number is
 * the one someone is actually deciding on.
 */
export function Backtest({
  ruleName,
  result,
  onResult,
}: {
  ruleName: string;
  result: BacktestResult | null;
  onResult: (result: BacktestResult | null) => void;
}) {
  const [days, setDays] = useState(120);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [showSql, setShowSql] = useState(false);

  async function run() {
    setBusy(true);
    setErrors([]);
    try {
      onResult(await api.backtest(ruleName, days, 20));
    } catch (exc) {
      setErrors(
        exc instanceof ApiError && exc.errors.length
          ? exc.errors
          : [exc instanceof ApiError ? exc.message : String(exc)],
      );
      onResult(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2>What would this have done?</h2>
          <p className="hint" style={{ marginBottom: 0 }}>
            Runs the rule over history. Nothing is sent.
          </p>
        </div>
        <div className="row">
          <input
            type="number"
            min={1}
            max={3650}
            value={days}
            style={{ width: 90 }}
            onChange={(e) => setDays(Number(e.target.value) || 1)}
            aria-label="Window in days"
          />
          <button className="btn primary" onClick={run} disabled={busy}>
            {busy ? "Running…" : "Backtest"}
          </button>
        </div>
      </div>

      {errors.length > 0 && (
        <div className="notice bad">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      {result && (
        <div style={{ opacity: busy ? 0.6 : 1, marginTop: 16 }}>
          <div className="hero">
            <span className="figure">{result.fires.toLocaleString()}</span>
            <span className="caption">
              times over {result.days} days, across{" "}
              {result.unique_keys.toLocaleString()} distinct key
              {result.unique_keys === 1 ? "" : "s"}
            </span>
          </div>
          <p className="footnote" style={{ marginBottom: 18 }}>
            {result.since?.slice(0, 10)} → {result.until?.slice(0, 10)} ·{" "}
            {result.query_ms} ms
          </p>

          <div className="kpi-row">
            <Stat
              label="Rows the query returned"
              value={result.matched_rows}
              sub={`trigger: ${result.trigger}`}
            />
            <Stat
              label="Collapsed by dedup"
              value={result.dedup_removed}
              sub={result.dedup_removed ? "duplicates suppressed" : "none"}
            />
            <Stat label="Mean per day" value={result.mean_per_day} />
            <Stat
              label="Busiest day"
              value={result.busiest_day ? result.busiest_day.count : 0}
              sub={result.busiest_day?.date}
            />
            <Stat
              label="Days with none"
              value={result.zero_days}
              sub={`of ${result.series.length}`}
            />
          </div>

          {result.timeless ? (
            <div className="notice warn">
              The trigger <strong>{result.trigger}</strong> declares no clock, so
              only totals are available — and it cannot run live.
            </div>
          ) : (
            <Chart series={result.series} label="fires" />
          )}

          {result.null_event_time_rows > 0 && (
            <div className="notice warn">
              <div className="notice-head">
                <span aria-hidden="true">⚠</span> {result.null_event_time_rows} rows
                match but have no timestamp
              </div>
              They cannot be placed on the timeline and are <strong>not</strong>{" "}
              counted above. A large share usually means the trigger names the
              wrong column as its clock.
            </div>
          )}

          <div className="notice warn">
            <div className="notice-head">
              <span aria-hidden="true">⚠</span> What this number is not
            </div>
            This looks at rows as they are <strong>now</strong> and uses the clock
            to place them in the past. It does not replay history, so it{" "}
            <strong>undercounts</strong>: an order completed in March and refunded
            in April is invisible here. Treat it as a floor.
          </div>

          {result.sample.length > 0 && (
            <>
              <div className="chart-head" style={{ marginTop: 18 }}>
                <span className="chart-title">
                  {result.sample.length} example rows
                </span>
              </div>
              <div className="table-scroll scroll-y">
                <table>
                  <thead>
                    <tr>
                      {result.columns.map((column) => (
                        <th key={column}>{column}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.sample.map((row, index) => (
                      <tr key={index}>
                        {result.columns.map((column) => {
                          const value = row[column];
                          if (value === null || value === undefined) {
                            return (
                              <td key={column}>
                                <span className="muted">null</span>
                              </td>
                            );
                          }
                          return (
                            <td
                              key={column}
                              className={typeof value === "number" ? "num" : undefined}
                            >
                              {formatCell(value, result.column_types[column])}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {result.sql && (
            <div style={{ marginTop: 16 }}>
              <button className="linkish" onClick={() => setShowSql((v) => !v)}>
                {showSql ? "hide" : "show"} the SQL this ran
              </button>
              {showSql && (
                <pre className="sql" style={{ marginTop: 10 }}>
                  {result.sql}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
