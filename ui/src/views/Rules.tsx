import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  api,
  type BacktestResult,
  type BindingInfo,
  type IntegrationInfo,
  type Policy,
  type RuleDraft,
  type RuleInfo,
  type RuleState,
  type SourceInfo,
  type TriggerInfo,
  NAME_PATTERN,
} from "../api";
import { Backtest } from "../components/Backtest";
import { Bindings } from "../components/Bindings";
import { BrandIcon } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { NameField } from "../components/NameField";
import { href, navigate } from "../router";

/* Rules: what to do when a trigger's rows appear, and how often.
 *
 * A rule is deliberately small -- pick a trigger, pick a cadence, attach an
 * action. Everything about *what* the event is already lives on the trigger,
 * so several rules can sit on one query and be promoted one at a time:
 * notifying ops can go live while texting the customer is still in shadow.
 *
 * The backtest sits inside the editor on purpose. You should never have to
 * save something and go elsewhere to find out what it would have done.
 */

const BLANK: RuleDraft = {
  name: "",
  trigger: "",
  description: null,
  policy: "once_ever",
  period: null,
  n: null,
};

function toDraft(rule: RuleInfo): RuleDraft {
  return {
    name: rule.name,
    trigger: rule.trigger,
    description: rule.description,
    policy: rule.policy as Policy,
    period: rule.period as RuleDraft["period"],
    n: rule.n,
  };
}

/** `selected` comes from the URL: /rules/<name>, or null for the new-rule form. */
export function Rules({ selected }: { selected: string | null }) {
  const [rules, setRules] = useState<RuleInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [triggers, setTriggers] = useState<TriggerInfo[]>([]);
  const [states, setStates] = useState<Record<string, RuleState>>({});
  const [integrations, setIntegrations] = useState<IntegrationInfo[]>([]);
  const [bindings, setBindings] = useState<BindingInfo[]>([]);
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const [defaultSource, setDefaultSource] = useState<string | null>(null);

  const [draft, setDraft] = useState<RuleDraft>(BLANK);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error` on purpose. These arrive from different actions
  // and a failed read rendered under "Not saved" tells the user their work was
  // lost when nothing was ever being written.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);

  const isNew = selected === null;

  const load = useCallback(async () => {
    try {
      const defs = await api.definitions();
      setRules(defs.rules);
      setTriggers(defs.triggers);
      setLoaded(true);
      {
        const [live, binds, configured, listed] = await Promise.all([
          api.rules(),
          api.bindings(),
          api.integrations(),
          api.sources(),
        ]);
        setStates(Object.fromEntries(live.rules.map((r) => [r.name, r])));
        setBindings(binds.bindings);
        setIntegrations(configured.integrations);
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

  // The control plane's reachability probe caches a negative briefly, so a
  // page opened during a restart can load into an error that is already stale.
  // One scheduled retry turns that into a blink rather than something the user
  // has to reload to clear.
  useEffect(() => {
    if (!loadError) return;
    const timer = window.setTimeout(load, 4000);
    return () => window.clearTimeout(timer);
  }, [loadError, load]);

  const trigger = useMemo(
    () => triggers.find((t) => t.name === draft.trigger),
    [triggers, draft.trigger],
  );

  /** The data source a trigger reads: the one it names, or the default. */
  const sourceOf = (name: string | undefined) => {
    const t = triggers.find((x) => x.name === name);
    return sources.find((s) => s.name === (t?.source ?? defaultSource)) ?? null;
  };
  const triggerSource = sourceOf(draft.trigger);

  // The columns a message template can use. Taken from the backtest when one
  // has been run, because that list came from the cursor and is therefore
  // exactly right -- including aliases and joined columns.
  const templateColumns = result?.columns ?? [];

  // The editor follows the URL. A rule's fields are copied into the draft
  // once per selection -- not on every reload of the list, which would throw
  // away an edit in progress whenever the page refreshed its data.
  const appliedFor = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!loaded || appliedFor.current === selected) return;
    if (selected === null) {
      setDraft({ ...BLANK, trigger: triggers[0]?.name ?? "" });
    } else {
      const rule = rules.find((r) => r.name === selected);
      if (!rule) return;
      setDraft(toDraft(rule));
    }
    appliedFor.current = selected;
    setResult(null);
    setSaved(null);
    setError(null);
  }, [loaded, selected, rules, triggers]);

  const missing = loaded && selected !== null && !rules.some((r) => r.name === selected);

  function startNew() {
    // Already on the new-rule form: clicking again starts it over.
    if (selected === null) appliedFor.current = undefined;
    navigate(href("rules"));
    if (selected === null) setDraft({ ...BLANK, trigger: triggers[0]?.name ?? "" });
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const response = await api.upsertRule(selected ?? draft.name, draft);
      setSaved(response.version ? `Saved as version ${response.version}.` : "Saved.");
      await load();
      // The draft already holds what was saved, so mark it applied rather
      // than let the URL change reset it. A rename replaces the old URL,
      // which no longer names anything; a new rule adds a history entry.
      appliedFor.current = draft.name;
      navigate(href("rules", draft.name), { replace: selected !== null });
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
      await api.deleteRule(name);
      await load();
      navigate(href("rules"), { replace: true });
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }

  // The name rule is checked here too, so the button cannot submit something
  // the API will only reject.
  const canSave = Boolean(draft.name && draft.trigger && NAME_PATTERN.test(draft.name));

  // Whether a key ever repeats is a fact about the data, not the schema, so it
  // is only knowable once a backtest has run. Until then this stays false and
  // nothing is claimed.
  const everyKeyIsUnique = Boolean(
    result && result.matched_rows > 0 && result.unique_keys === result.matched_rows,
  );
  const state = states[draft.name];

  if (!loaded && !loadError) return null;

  if (triggers.length === 0 && !loadError) {
    return (
      <div className="card">
        <h2>No triggers yet</h2>
        <p className="hint">
          A rule fires on a trigger's rows, so there has to be a trigger first.
          Write a query under <strong>Triggers</strong> and come back.
        </p>
      </div>
    );
  }

  return (
    <div className="rules">
      <aside className="rule-list">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
          <h2>All rules</h2>
          <button className="btn primary" onClick={startNew}>
            New rule
          </button>
        </div>

        {rules.length === 0 && (
          <p className="hint">None yet. Create one to see what it would have done.</p>
        )}

        {rules.map((rule) => {
          const live = states[rule.name];
          return (
            <Link
              key={rule.name}
              to={href("rules", rule.name)}
              className={`rule-card${selected === rule.name ? " active" : ""}`}
              aria-current={selected === rule.name ? "page" : undefined}
            >
              <div className="rule-card-head">
                <strong>{rule.name}</strong>
                {live && (
                  <span className={`tag ${live.mode === "live" ? "live" : ""}`}>
                    {live.enabled ? live.mode : "disabled"}
                  </span>
                )}
              </div>
              <div className="muted with-icon" style={{ gap: 6 }}>
                <BrandIcon name={sourceOf(rule.trigger)?.kind} size="sm" label="" />
                on {rule.trigger}
              </div>
              {live && live.total_fires > 0 && (
                <div className="muted" style={{ fontSize: 11 }}>
                  {live.total_fires.toLocaleString()} fired
                </div>
              )}
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
            <h2>No rule called {selected}</h2>
            <p className="hint">
              It may have been renamed or deleted.{" "}
              <Link to={href("rules")} className="ref">
                Start a new rule
              </Link>{" "}
              or pick one from the list.
            </p>
          </div>
        ) : (
        <>
        <div className="card">
          <h2>{isNew ? "New rule" : draft.name}</h2>
          <p className="hint">
            Pick the event, then say how often one key may fire. The query, its
            clock and its grain all come from the trigger.
          </p>

          <div className="grid-2">
            <NameField
              id="rule-name"
              value={draft.name}
              placeholder="thank_the_customer"
              onChange={(name) => setDraft({ ...draft, name })}
            />
            <div>
              <label className="field" htmlFor="rule-trigger">
                Trigger — when this fires
              </label>
              <select
                id="rule-trigger"
                value={draft.trigger}
                onChange={(e) => setDraft({ ...draft, trigger: e.target.value })}
              >
                <option value="">choose…</option>
                {triggers.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {trigger && (
            <div className="notice" style={{ marginTop: 12 }}>
              <div className="notice-head">
                <Link to={href("triggers", trigger.name)} className="ref">
                  {trigger.name}
                </Link>
              </div>
              {triggerSource && (
                <div className="with-icon" style={{ margin: "4px 0 6px", gap: 6 }}>
                  <BrandIcon name={triggerSource.kind} size="sm" label="" />
                  <span className="muted">
                    reads{" "}
                    <Link to={href("sources", triggerSource.name)} className="ref">
                      {triggerSource.name}
                    </Link>{" "}
                    · {triggerSource.label}
                  </span>
                </div>
              )}
              {trigger.description && <div>{trigger.description}</div>}
              <div className="muted" style={{ marginTop: 6 }}>
                one row per <strong>{trigger.key.join(" + ")}</strong>
                {trigger.event_time ? (
                  <>
                    , placed in time by <strong>{trigger.event_time}</strong>
                  </>
                ) : (
                  <> — no clock, so this cannot run live</>
                )}
              </div>
            </div>
          )}

          <div style={{ marginTop: 12 }}>
            <label className="field" htmlFor="rule-desc">
              Description
            </label>
            <input
              id="rule-desc"
              type="text"
              value={draft.description ?? ""}
              placeholder="At most one thank-you per customer per week"
              onChange={(e) => setDraft({ ...draft, description: e.target.value || null })}
            />
          </div>

          <div style={{ marginTop: 12 }}>
            <label className="field" htmlFor="rule-policy">
              Fire at most
            </label>
            <select
              id="rule-policy"
              value={draft.policy}
              onChange={(e) => {
                const policy = e.target.value as Policy;
                setDraft({
                  ...draft,
                  policy,
                  period: policy === "once_per_period" ? "week" : null,
                  n: policy === "once_per_n" ? 3 : null,
                });
              }}
            >
              <option value="once_ever">once ever, per key</option>
              <option value="once_per_period">once per period, per key</option>
              <option value="once_per_n">every nth, per key</option>
            </select>

            {draft.policy === "once_per_period" && (
              <select
                style={{ marginTop: 8 }}
                value={draft.period ?? "week"}
                onChange={(e) =>
                  setDraft({ ...draft, period: e.target.value as RuleDraft["period"] })
                }
              >
                <option value="day">per day</option>
                <option value="week">per week</option>
                <option value="month">per month</option>
              </select>
            )}

            {draft.policy === "once_per_n" && (
              <input
                style={{ marginTop: 8 }}
                type="number"
                min={1}
                value={draft.n ?? 3}
                onChange={(e) => setDraft({ ...draft, n: Number(e.target.value) })}
              />
            )}

            {trigger && (
              <p className="footnote" style={{ marginTop: 8 }}>
                {describePolicy(draft, trigger.key.join(" + "))}
              </p>
            )}

            {draft.policy === "once_per_n" && trigger && !trigger.event_time && (
              <div className="notice bad" style={{ marginTop: 8 }}>
                Every nth needs a clock to tell one occurrence from the next, and{" "}
                <strong>{trigger.name}</strong> has none. Give the trigger an
                event_time, or choose another policy.
              </div>
            )}

            {draft.policy === "once_per_n" && everyKeyIsUnique && (
              // Not a guess: this is what the backtest just measured. Every
              // key occurred exactly once, so the count never reaches the nth
              // and the rule fires on everything -- "every 3rd" behaving as
              // "every one" is precisely the kind of quietly-wrong rule this
              // product exists to catch before it ships.
              <div className="notice warn" style={{ marginTop: 8 }}>
                <div className="notice-head">
                  <span aria-hidden="true">⚠</span> This would fire on every row
                </div>
                Over the last {result?.days} days every one of{" "}
                <strong>{result?.matched_rows.toLocaleString()}</strong> rows had a
                different <strong>{trigger?.key.join(" + ")}</strong>, so no key ever
                reaches a {ordinal(draft.n ?? 3)} occurrence. Every nth only differs
                from <em>once ever</em> when a key comes back more than once — key
                the trigger on something that repeats, like the customer rather than
                the order.
              </div>
            )}
          </div>

          <div className="row" style={{ marginTop: 16 }}>
            <button className="btn primary" onClick={save} disabled={!canSave || saving}>
              {saving ? "Saving…" : isNew ? "Create rule" : "Save changes"}
            </button>
            {!isNew && (
              <button className="linkish" onClick={() => remove(draft.name)}>
                delete
              </button>
            )}
            {saved && <span className="muted">{saved}</span>}
          </div>

          {!isNew && state?.mode === "live" && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              This rule is <strong>live</strong>. Changing what it means sends it
              back to shadow, and you promote it again when you are happy.
            </div>
          )}
        </div>

        {!isNew && (
          <>
            <Backtest ruleName={draft.name} result={result} onResult={setResult} />
            {(
              <Bindings
                ruleName={draft.name}
                integrations={integrations}
                bindings={bindings.filter((b) => b.rule_name === draft.name)}
                templateColumns={templateColumns}
                onChanged={load}
              />
            )}
          </>
        )}
        </>
        )}
      </section>
    </div>
  );
}

/** Say the policy back in the trigger's own terms, not in jargon. */
function describePolicy(draft: RuleDraft, grain: string): string {
  switch (draft.policy) {
    case "once_per_period":
      return `One fire per ${grain} per ${draft.period ?? "week"}, however many rows appear.`;
    case "once_per_n":
      return `One fire on every ${ordinal(draft.n ?? 3)} row for a given ${grain}.`;
    default:
      return `One fire per ${grain}, ever. Seeing it again never fires.`;
  }
}

function ordinal(n: number): string {
  const suffix = n % 10 === 1 && n % 100 !== 11 ? "st"
    : n % 10 === 2 && n % 100 !== 12 ? "nd"
    : n % 10 === 3 && n % 100 !== 13 ? "rd"
    : "th";
  return `${n}${suffix}`;
}
