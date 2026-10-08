import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  api,
  type Activity,
  type Limits,
  type RuleStates,
  type TriggerStates,
} from "../api";
import { BrandIcon } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { href } from "../router";

/* The operator view: what is running, how far it has got, what it has fired.
 *
 * Two tables, because the system really does have two clocks. Scheduling
 * belongs to the **trigger** -- one query, leased once, polled once, however
 * many rules read it. Mode belongs to the **rule**, so one automation can be
 * promoted while its sibling on the same query is still being watched.
 */

const REFRESH_MS = 5000;

function ago(iso: string | null): string {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function when(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toISOString().replace("T", " ").slice(0, 16);
}

export function Live() {
  const [triggers, setTriggers] = useState<TriggerStates | null>(null);
  const [rules, setRules] = useState<RuleStates | null>(null);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [rewinding, setRewinding] = useState<string | null>(null);
  const [rewindDays, setRewindDays] = useState(7);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Which engine each trigger reads, for its mark. Fetched once rather than
  // on every refresh: it changes when someone edits a trigger, not by itself.
  const [engineOf, setEngineOf] = useState<Record<string, string | null>>({});

  useEffect(() => {
    api
      .sources()
      .then((listed) =>
        setEngineOf(
          Object.fromEntries(
            listed.sources.flatMap((source) => source.triggers.map((t) => [t, source.kind])),
          ),
        ),
      )
      .catch(() => undefined);
  }, []);

  const load = useCallback(async () => {
    try {
      const [nextTriggers, nextRules, acts, caps] = await Promise.all([
        api.triggers(),
        api.rules(),
        api.activity(),
        api.limits(),
      ]);
      setTriggers(nextTriggers);
      setRules(nextRules);
      setActivity(acts);
      setLimits((current) => current ?? caps);
      setError(null);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function toggle(name: string, mode: "shadow" | "live") {
    setBusy(name);
    try {
      await api.setMode(name, mode);
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  async function setHalted(halted: boolean) {
    setBusy("__workspace__");
    try {
      if (halted) await api.halt("stopped from the UI");
      else await api.resume();
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  if (error && !rules) {
    return (
      <div className="card">
        <div className="notice bad">{error}</div>
      </div>
    );
  }

  if (!rules || !triggers) {
    return (
      <div className="card">
        <p className="hint">Loading…</p>
      </div>
    );
  }

  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h2>Rules</h2>
            <p className="hint" style={{ marginBottom: 0 }}>
              Rules run in shadow until you promote them — fully rendered and
              recorded, but not sent. Editing a rule, or the trigger it reads,
              sends it back to shadow.
            </p>
          </div>
          <button
            className="btn"
            onClick={() => setHalted(!rules.halted)}
            disabled={busy === "__workspace__"}
          >
            {rules.halted ? "Resume everything" : "Stop everything"}
          </button>
        </div>

        {rules.halted && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">■</span> Halted
            </div>
            {rules.halted_reason || "No reason given."} Nothing will fire until you
            resume.
          </div>
        )}

        {error && <div className="notice bad">{error}</div>}

        <div className="table-scroll" style={{ marginTop: 16 }}>
          <table>
            <thead>
              <tr>
                <th>rule</th>
                <th>trigger</th>
                <th>mode</th>
                <th>fires</th>
                <th>last fired</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rules.rules.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    No rules yet — create one first.
                  </td>
                </tr>
              )}
              {rules.rules.map((rule) => (
                <tr key={rule.name}>
                  <td>
                    <Link to={href("rules", rule.name)} className="ref">
                      {rule.name}
                    </Link>
                    {rule.last_error && (
                      <div
                        className="muted"
                        style={{ fontSize: 11, whiteSpace: "normal", maxWidth: 260 }}
                      >
                        {rule.last_error}
                      </div>
                    )}
                  </td>
                  <td className="muted">
                    <span className="with-icon" style={{ gap: 6 }}>
                      <BrandIcon name={engineOf[rule.trigger]} size="sm" label="" />
                      <Link to={href("triggers", rule.trigger)} className="ref">
                        {rule.trigger}
                      </Link>
                    </span>
                  </td>
                  <td>
                    <span className={`tag ${rule.mode === "live" ? "live" : ""}`}>
                      {rule.enabled ? rule.mode : "disabled"}
                    </span>
                  </td>
                  <td className="num">{rule.total_fires.toLocaleString()}</td>
                  <td className="num">{ago(rule.last_fired_at)}</td>
                  <td>
                    <button
                      className="linkish"
                      disabled={busy === rule.name || !rule.enabled}
                      onClick={() =>
                        toggle(rule.name, rule.mode === "live" ? "shadow" : "live")
                      }
                    >
                      {rule.mode === "live" ? "return to shadow" : "promote to live"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {limits && (
        <div className="card">
          <h2>Frequency cap</h2>
          <p className="hint">
            The backstop against a runaway rule: at most this many sends to one
            recipient in this window. Anything over it is recorded as suppressed
            rather than dropped silently, so you can see what it stopped.
          </p>
          <div className="row" style={{ marginTop: 12, alignItems: "flex-end" }}>
            <div>
              <label className="field" htmlFor="cap-count">
                Sends per recipient
              </label>
              <input
                id="cap-count"
                type="number"
                min={1}
                style={{ width: 120 }}
                value={limits.cap_per_recipient}
                onChange={(e) =>
                  setLimits({ ...limits, cap_per_recipient: Number(e.target.value) || 1 })
                }
              />
            </div>
            <div>
              <label className="field" htmlFor="cap-window">
                Per how many hours
              </label>
              <input
                id="cap-window"
                type="number"
                min={1}
                style={{ width: 120 }}
                value={limits.cap_window_hours}
                onChange={(e) =>
                  setLimits({ ...limits, cap_window_hours: Number(e.target.value) || 1 })
                }
              />
            </div>
            <button
              className="btn"
              disabled={busy === "__limits__"}
              onClick={async () => {
                setBusy("__limits__");
                try {
                  setLimits(await api.setLimits(limits));
                } catch (exc) {
                  setError(exc instanceof ApiError ? exc.message : String(exc));
                } finally {
                  setBusy(null);
                }
              }}
            >
              Save
            </button>
          </div>
          <p className="footnote" style={{ marginTop: 10 }}>
            What counts as "one recipient" is set per action, on the rule. Left
            unset it is the whole integration, so a rule writing to one Slack
            channel shares a single allowance.
          </p>
        </div>
      )}

      <div className="card">
        <h2>Triggers</h2>
        <p className="hint">
          Each query is polled once and its rows fanned out to every rule on it —
          cheaper for your database, and the only way two rules see the same
          snapshot.
        </p>

        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>trigger</th>
                <th>caught up to</th>
                <th>next poll</th>
                <th>every</th>
                <th>rules</th>
                <th>fires</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {triggers.triggers.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    No triggers yet — save some definitions first.
                  </td>
                </tr>
              )}
              {triggers.triggers.map((trigger) => (
                <tr key={trigger.name}>
                  <td>
                    <span className="with-icon" style={{ gap: 6 }}>
                      <BrandIcon name={engineOf[trigger.name]} size="sm" label="" />
                      <Link to={href("triggers", trigger.name)} className="ref">
                        {trigger.name}
                      </Link>
                    </span>
                    {trigger.last_error && (
                      <div
                        className="muted"
                        style={{ fontSize: 11, whiteSpace: "normal", maxWidth: 260 }}
                      >
                        {trigger.last_error}
                      </div>
                    )}
                  </td>
                  <td className="num">{when(trigger.watermark)}</td>
                  <td className="num">{ago(trigger.next_run_at)}</td>
                  <td className="num muted">{trigger.poll_interval_seconds}s</td>
                  <td className="muted">
                    {trigger.rules.length === 0 ? (
                      "none"
                    ) : (
                      <>
                        {trigger.rules.length}
                        {trigger.live_rules.length > 0 && (
                          <span className="tag live" style={{ marginLeft: 6 }}>
                            {trigger.live_rules.length} live
                          </span>
                        )}
                      </>
                    )}
                  </td>
                  <td className="num">{trigger.total_fires.toLocaleString()}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button
                      className="linkish"
                      disabled={busy === trigger.name}
                      onClick={async () => {
                        setBusy(trigger.name);
                        try {
                          await api.runTrigger(trigger.name);
                          await load();
                        } catch (exc) {
                          setError(exc instanceof ApiError ? exc.message : String(exc));
                        } finally {
                          setBusy(null);
                        }
                      }}
                    >
                      run now
                    </button>{" "}
                    <button
                      className="linkish"
                      onClick={() =>
                        setRewinding(rewinding === trigger.name ? null : trigger.name)
                      }
                    >
                      check further back
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {rewinding && (
        <div className="card">
          <h2>Check further back: {rewinding}</h2>
          <p className="hint">
            Moves this trigger's watermark back so the next poll re-reads older
            rows. Useful for testing a rule written after the data it should
            have fired on.
          </p>
          <div className="notice" style={{ marginTop: 10 }}>
            <div className="notice-head">What this does and does not do</div>
            Re-reading is not re-firing. Anything already in the fire ledger is
            recognised and skipped, so this finds only what a rule has never
            seen.
            {(triggers.triggers.find((t) => t.name === rewinding)?.live_rules.length ?? 0) >
            0 ? (
              <div style={{ marginTop: 8 }}>
                <strong>
                  {triggers.triggers.find((t) => t.name === rewinding)?.live_rules.join(", ")}
                </strong>{" "}
                {triggers.triggers.find((t) => t.name === rewinding)!.live_rules.length === 1
                  ? "is live on this trigger and will send for anything it finds."
                  : "are live on this trigger and will send for anything they find."}
              </div>
            ) : (
              <div style={{ marginTop: 8 }}>
                Every rule on this trigger is in shadow, so nothing will be sent.
              </div>
            )}
          </div>

          <div className="row" style={{ marginTop: 12, alignItems: "flex-end" }}>
            <div>
              <label className="field" htmlFor="rewind-days">
                How many days back
              </label>
              <input
                id="rewind-days"
                type="number"
                min={1}
                max={3650}
                style={{ width: 120 }}
                value={rewindDays}
                onChange={(e) => setRewindDays(Number(e.target.value) || 1)}
              />
            </div>
            <button
              className="btn"
              disabled={busy === rewinding}
              onClick={async () => {
                const name = rewinding;
                setBusy(name);
                try {
                  await api.rewindTrigger(name, rewindDays);
                  setRewinding(null);
                  await load();
                } catch (exc) {
                  setError(exc instanceof ApiError ? exc.message : String(exc));
                } finally {
                  setBusy(null);
                }
              }}
            >
              Rewind and poll
            </button>
            <button className="linkish" onClick={() => setRewinding(null)}>
              cancel
            </button>
          </div>
        </div>
      )}

      <div className="card">
        <h2>Recent activity</h2>
        <p className="hint">
          Every poll is recorded, whether it fired anything or not — so “nothing
          happened” is distinguishable from “nothing ran”. A run has no mode of
          its own: the rules on it do, and <strong>sent</strong> is how many
          messages actually left.
        </p>

        <div className="table-scroll scroll-y">
          <table>
            <thead>
              <tr>
                <th>when</th>
                <th>trigger</th>
                <th>matched</th>
                <th>fired</th>
                <th>already fired</th>
                <th>sent</th>
                <th>status</th>
              </tr>
            </thead>
            <tbody>
              {(activity?.runs.length ?? 0) === 0 && (
                <tr>
                  <td colSpan={7} className="muted">
                    No runs yet. Start the worker to begin polling.
                  </td>
                </tr>
              )}
              {activity?.runs.map((run, index) => (
                <tr key={index}>
                  <td className="num">{ago(run.started_at)}</td>
                  <td>
                    <span className="with-icon" style={{ gap: 6 }}>
                      <BrandIcon name={engineOf[run.trigger]} size="sm" label="" />
                      <Link to={href("triggers", run.trigger)} className="ref">
                        {run.trigger}
                      </Link>
                    </span>
                  </td>
                  <td className="num">{run.matched_rows}</td>
                  <td className="num">
                    <strong>{run.fires_new}</strong>
                  </td>
                  <td className="num muted">{run.fires_suppressed}</td>
                  <td className="num">
                    {run.sent > 0 ? (
                      <span className="tag live">{run.sent}</span>
                    ) : (
                      <span className="muted">0</span>
                    )}
                  </td>
                  <td className={run.status === "failed" ? "bad-text" : "muted"}>
                    {run.error ? run.error.slice(0, 40) : run.status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {(activity?.fires.length ?? 0) > 0 && (
        <div className="card">
          <h2>Recent fires</h2>
          <p className="hint">
            The ledger, per rule. A row here is a permanent record that this rule
            fired for this key — which is what makes a duplicate send impossible
            rather than unlikely.
          </p>
          <div className="table-scroll scroll-y">
            <table>
              <thead>
                <tr>
                  <th>when</th>
                  <th>rule</th>
                  <th>key</th>
                  <th>event time</th>
                </tr>
              </thead>
              <tbody>
                {activity?.fires.map((fire, index) => (
                  <tr key={index}>
                    <td className="num">{ago(fire.fired_at)}</td>
                    <td>
                      <Link to={href("rules", fire.rule)} className="ref">
                        {fire.rule}
                      </Link>
                    </td>
                    <td className="num">{fire.entity_id ?? "—"}</td>
                    <td className="num muted">{when(fire.event_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
