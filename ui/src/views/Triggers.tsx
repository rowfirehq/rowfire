import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  api,
  type CheckResult,
  type RuleInfo,
  type SourceInfo,
  type TableInfo,
  type TriggerDraft,
  type TriggerInfo,
  type TriggerState,
  NAME_PATTERN,
} from "../api";
import { BrandIcon } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { NameField } from "../components/NameField";
import { SchemaBrowser } from "../components/SchemaBrowser";
import { href, navigate } from "../router";

/* Triggers: write the query that says when something happened.
 *
 * The loop this screen exists for: type SQL, see the columns it returns, then
 * name the clock and the key from that list. The columns come back from the
 * database -- the query is run with LIMIT 0 -- so aliases and joined columns
 * are offered as choices, which no amount of parsing would get right.
 *
 * The key lives here and not on the rules on purpose. "One row per order,
 * identified by id" is a fact about the query, and the person who wrote the
 * join is the only one who reliably knows where duplication can happen.
 */

const BLANK: TriggerDraft = {
  name: "",
  description: null,
  sql: "",
  event_time: null,
  key: [],
  source: null,
};

const EXAMPLE = `SELECT o.id,
       o.customer_id,
       o.completed_at,
       o.total_amount,
       c.first_name,
       c.phone
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.status IN (4, 7)
  AND o.is_test = false`;

function toDraft(trigger: TriggerInfo): TriggerDraft {
  return {
    name: trigger.name,
    description: trigger.description,
    sql: trigger.sql,
    event_time: trigger.event_time,
    key: trigger.key,
    source: trigger.source,
  };
}

/** `selected` comes from the URL: /triggers/<name>, or null for the new-trigger form. */
export function Triggers({ selected }: { selected: string | null }) {
  const [triggers, setTriggers] = useState<TriggerInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [rules, setRules] = useState<RuleInfo[]>([]);
  const [states, setStates] = useState<Record<string, TriggerState>>({});
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const [defaultSource, setDefaultSource] = useState<string | null>(null);

  const [draft, setDraft] = useState<TriggerDraft>(BLANK);
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error` on purpose. These arrive from different actions
  // and a failed read rendered under "Not saved" tells the user their work was
  // lost when nothing was ever being written.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showSql, setShowSql] = useState(false);

  const isNew = selected === null;

  const load = useCallback(async () => {
    try {
      const defs = await api.definitions();
      setTriggers(defs.triggers);
      setRules(defs.rules);
      setLoaded(true);
      {
        const [live, listed] = await Promise.all([api.triggers(), api.sources()]);
        setStates(Object.fromEntries(live.triggers.map((t) => [t.name, t])));
        setSources(listed.sources);
        setDefaultSource(listed.default);
      }
      setLoadError(null);
    } catch (exc) {
      setLoadError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // The source this query runs against: the one it names, or the default.
  const sourceName = draft.source ?? defaultSource;
  const source = sources.find((s) => s.name === sourceName) ?? null;
  const kindOf = (name: string | null) =>
    sources.find((s) => s.name === (name ?? defaultSource))?.kind ?? null;

  // The schema reference follows the source being written against.
  useEffect(() => {
    if (!sourceName) return;
    api
      .schema(sourceName)
      .then((s) => setTables(s.tables))
      .catch(() => setTables([]));
  }, [sourceName]);

  // The control plane's reachability probe caches a negative briefly, so a
  // page opened during a restart can load into an error that is already stale.
  // One scheduled retry turns that into a blink rather than something the user
  // has to reload to clear.
  useEffect(() => {
    if (!loadError) return;
    const timer = window.setTimeout(load, 4000);
    return () => window.clearTimeout(timer);
  }, [loadError, load]);

  // Check as you type, but not on every keystroke: this runs the query
  // against the database, so it is debounced.
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!draft.sql.trim()) {
      setCheck(null);
      return;
    }
    window.clearTimeout(timer.current);
    setChecking(true);
    timer.current = window.setTimeout(() => {
      api
        .checkSql(draft.sql, draft.event_time, draft.key, draft.source)
        .then(setCheck)
        .catch((exc) =>
          setCheck({
            valid: false,
            errors: [exc instanceof ApiError ? exc.message : String(exc)],
            columns: [],
            sql: null,
          }),
        )
        .finally(() => setChecking(false));
    }, 500);
    return () => window.clearTimeout(timer.current);
  }, [draft.sql, draft.event_time, draft.key, draft.source]);

  /** Point the draft at a source, without needlessly rewriting what it names. */
  function chooseSource(name: string) {
    const original = triggers.find((t) => t.name === selected);
    // An existing trigger keeps the spelling it had: turning an implicit
    // default into an explicit one would change its checksum and send its
    // rules back to shadow for nothing.
    const explicit =
      name === original?.source ? name : name === defaultSource ? null : name;
    setDraft({ ...draft, source: explicit });
  }

  // The editor follows the URL. A trigger's fields are copied into the draft
  // once per selection -- not on every reload of the list, which would throw
  // away a query being edited whenever the page refreshed its data.
  const appliedFor = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!loaded || appliedFor.current === selected) return;
    if (selected === null) {
      setDraft({ ...BLANK, sql: EXAMPLE });
    } else {
      const trigger = triggers.find((t) => t.name === selected);
      if (!trigger) return;
      setDraft(toDraft(trigger));
    }
    appliedFor.current = selected;
    setCheck(null);
    setSaved(null);
    setError(null);
  }, [loaded, selected, triggers]);

  const missing = loaded && selected !== null && !triggers.some((t) => t.name === selected);

  function startNew() {
    // Already on the new-trigger form: clicking again starts it over.
    if (selected === null) {
      appliedFor.current = undefined;
      setDraft({ ...BLANK, sql: EXAMPLE });
    }
    navigate(href("triggers"));
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const response = await api.upsertTrigger(selected ?? draft.name, draft);
      setSaved(
        response.version ? `Saved as version ${response.version}.` : "Saved.",
      );
      await load();
      // The draft already holds what was saved, so mark it applied rather
      // than let the URL change reset it. A rename replaces the old URL,
      // which no longer names anything; a new trigger adds a history entry.
      appliedFor.current = draft.name;
      navigate(href("triggers", draft.name), { replace: selected !== null });
    } catch (exc) {
      setError(
        exc instanceof ApiError
          ? exc.errors.length
            ? exc.errors.join(" · ")
            : exc.message
          : String(exc),
      );
    } finally {
      setSaving(false);
    }
  }

  async function remove(name: string) {
    setError(null);
    try {
      await api.deleteTrigger(name);
      await load();
      navigate(href("triggers"), { replace: true });
    } catch (exc) {
      setError(
        exc instanceof ApiError
          ? exc.errors.length
            ? exc.errors.join(" · ")
            : exc.message
          : String(exc),
      );
    }
  }

  const columns = check?.columns ?? [];
  const timestampColumns = columns.filter((c) => c.type === "timestamp");
  const dependents = rules.filter((r) => r.trigger === draft.name);
  // Save needs a name and a query that ran; the clock and key are checked by
  // the API against the columns the query returned.
  const canSave = Boolean(
    draft.name && NAME_PATTERN.test(draft.name) && draft.sql.trim() && check?.valid,
  );

  return (
    <div className="rules">
      <aside className="rule-list">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
          <h2>All triggers</h2>
          <button className="btn primary" onClick={startNew}>
            New trigger
          </button>
        </div>

        {triggers.length === 0 && (
          <p className="hint">
            None yet. Write a SELECT and this will tell you what it returns.
          </p>
        )}

        {triggers.map((trigger) => {
          const state = states[trigger.name];
          const using = rules.filter((r) => r.trigger === trigger.name);
          return (
            <Link
              key={trigger.name}
              to={href("triggers", trigger.name)}
              className={`rule-card${selected === trigger.name ? " active" : ""}`}
              aria-current={selected === trigger.name ? "page" : undefined}
            >
              <div className="rule-card-head">
                <span className="with-icon">
                  <BrandIcon
                    name={kindOf(trigger.source)}
                    size="sm"
                    label={trigger.source ?? defaultSource ?? ""}
                  />
                  <strong>{trigger.name}</strong>
                </span>
                {state && state.live_rules.length > 0 && (
                  <span className="tag live">{state.live_rules.length} live</span>
                )}
              </div>
              <div className="muted">
                key: {trigger.key.join(", ")} · clock: {trigger.event_time ?? "none"}
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                {using.length === 0
                  ? "no rules yet"
                  : `${using.length} rule${using.length === 1 ? "" : "s"}`}
              </div>
            </Link>
          );
        })}
      </aside>

      <section className="rule-detail">
        {loadError && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Could not load
            </div>
            {loadError}
            <div className="row" style={{ marginTop: 10 }}>
              <button className="btn" onClick={() => load()}>
                Try again
              </button>
            </div>
          </div>
        )}

        {error && (
          <div className="notice bad">
            <div className="notice-head">
              <span aria-hidden="true">✕</span> Not saved
            </div>
            {error}
          </div>
        )}

        {missing ? (
          <div className="card">
            <h2>No trigger called {selected}</h2>
            <p className="hint">
              It may have been renamed or deleted.{" "}
              <Link to={href("triggers")} className="ref">
                Write a new trigger
              </Link>{" "}
              or pick one from the list.
            </p>
          </div>
        ) : (
        <div className="card">
          <h2>{isNew ? "New trigger" : draft.name}</h2>
          <p className="hint">
            Any single SELECT. Join whatever you need — a joined column is just a
            column, available to every rule and every message template, with no
            separate enrichment step.
          </p>

          <div className="grid-2">
            <NameField
              id="trigger-name"
              value={draft.name}
              placeholder="order_completed"
              onChange={(name) => setDraft({ ...draft, name })}
            />
            <div>
              <label className="field" htmlFor="trigger-desc">
                Description
              </label>
              <input
                id="trigger-desc"
                type="text"
                value={draft.description ?? ""}
                placeholder="An order finished and was paid for"
                onChange={(e) =>
                  setDraft({ ...draft, description: e.target.value || null })
                }
              />
            </div>
          </div>

          <div style={{ marginTop: 12 }}>
            <span className="field" id="trigger-source-label">
              Data source — the database this query reads
            </span>
            <div className="source-picker" role="radiogroup" aria-labelledby="trigger-source-label">
              {sources.map((option) => (
                <button
                  key={option.name}
                  type="button"
                  role="radio"
                  aria-checked={option.name === sourceName}
                  className={`source-option${option.name === sourceName ? " on" : ""}`}
                  onClick={() => chooseSource(option.name)}
                >
                  <BrandIcon name={option.kind} size="sm" label="" />
                  <span>
                    <strong>{option.name}</strong>
                    <span className="muted"> · {option.label}</span>
                  </span>
                  {option.default && <span className="tag">default</span>}
                </button>
              ))}
            </div>
            {source && (
              <p className="footnote" style={{ marginTop: 6 }}>
                Written in {source.label}'s dialect
                {source.kind === "mysql" ? " — backticks and MySQL functions are fine" : ""}.{" "}
                <Link to={href("sources", source.name)} className="ref">
                  {source.summary}
                </Link>
              </p>
            )}
          </div>

          <div style={{ marginTop: 12 }}>
            <label className="field" htmlFor="trigger-sql">
              Query
            </label>
            <textarea
              id="trigger-sql"
              value={draft.sql}
              spellCheck={false}
              className="sql-editor"
              placeholder={EXAMPLE}
              onChange={(e) => setDraft({ ...draft, sql: e.target.value })}
            />
            <p className="footnote">
              Read-only: a write is refused before it reaches the database, and
              the connection could not perform one anyway. The time window is
              added around your query — you do not filter by time yourself.
            </p>
          </div>

          {checking && <p className="hint">Checking…</p>}

          {check && !check.valid && check.errors.length > 0 && (
            <div className="notice bad">
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {check.errors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            </div>
          )}

          {columns.length > 0 && (
            <>
              <div className="chart-head" style={{ marginTop: 18 }}>
                <span className="chart-title">
                  this query returns {columns.length} column
                  {columns.length === 1 ? "" : "s"}
                </span>
              </div>
              <div className="picker" style={{ marginTop: 8 }}>
                {columns.map((column) => (
                  <span key={column.name} className="tag">
                    {column.name} <span className="muted">{column.type}</span>
                  </span>
                ))}
              </div>

              <div className="grid-2" style={{ marginTop: 16 }}>
                <div>
                  <label className="field" htmlFor="trigger-clock">
                    Clock — which column places a row in time
                  </label>
                  <select
                    id="trigger-clock"
                    value={draft.event_time ?? ""}
                    onChange={(e) =>
                      setDraft({ ...draft, event_time: e.target.value || null })
                    }
                  >
                    <option value="">none (totals only, cannot run live)</option>
                    {(timestampColumns.length ? timestampColumns : columns).map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                  <p className="footnote">
                    The watermark moves along this column, so without one every
                    poll would re-scan everything.
                  </p>
                </div>
                <div>
                  <label className="field">Key — what makes one row one thing</label>
                  <div className="picker">
                    {columns.map((column) => (
                      <label key={column.name} className="pick">
                        <input
                          type="checkbox"
                          checked={draft.key.includes(column.name)}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              key: e.target.checked
                                ? [...draft.key, column.name]
                                : draft.key.filter((c) => c !== column.name),
                            })
                          }
                        />
                        {column.name}
                      </label>
                    ))}
                  </div>
                  <p className="footnote">
                    Pick <code>id</code> to dedup per order, or{" "}
                    <code>customer_id</code> to dedup per customer. Every rule on
                    this trigger counts at this grain.
                  </p>
                </div>
              </div>
            </>
          )}

          {check?.sql && (
            <div style={{ marginTop: 14 }}>
              <button className="linkish" onClick={() => setShowSql((v) => !v)}>
                {showSql ? "hide" : "show"} the query that will actually run
              </button>
              {showSql && <pre className="sql" style={{ marginTop: 10 }}>{check.sql}</pre>}
            </div>
          )}

          <div className="row" style={{ marginTop: 16 }}>
            <button className="btn primary" onClick={save} disabled={!canSave || saving}>
              {saving ? "Saving…" : isNew ? "Create trigger" : "Save changes"}
            </button>
            {!isNew && (
              <button className="linkish" onClick={() => remove(draft.name)}>
                delete
              </button>
            )}
            {saved && <span className="muted">{saved}</span>}
          </div>

          {!isNew && dependents.length > 0 && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              {dependents.length} rule{dependents.length === 1 ? "" : "s"} read this
              trigger:{" "}
              {dependents.map((r, i) => (
                <span key={r.name}>
                  {i > 0 && ", "}
                  <Link to={href("rules", r.name)} className="ref">
                    {r.name}
                  </Link>
                </span>
              ))}
              . Changing the query changes what all of them fire for, so they all
              go back to shadow.
            </div>
          )}
        </div>
        )}

        {tables.length > 0 && (
          <details className="card">
            <summary>
              <strong>Schema reference</strong>{" "}
              <span className="muted">{tables.length} tables</span>
            </summary>
            <p className="hint" style={{ marginTop: 10 }}>
              What is available to join. Read live from the database, so it is
              what your query will actually run against.
            </p>
            <SchemaBrowser tables={tables} />
          </details>
        )}
      </section>
    </div>
  );
}
