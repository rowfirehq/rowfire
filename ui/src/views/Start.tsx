import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  api,
  type BacktestResult,
  type InboxItem,
  type SourceInfo,
  type SuggestedSource,
  type TriggerInfo,
} from "../api";
import { BrandIcon } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { SimulateButton } from "../components/SimulateButton";
import { moment } from "../moments";
import { href } from "../router";
import { InboxEntry } from "./Inbox";

/* Get started: one rule, end to end, in a couple of minutes.
 *
 * Built for the person the README promises can do this without engineering.
 * Somebody who knows the schema has already described the events (triggers);
 * this walks the rest: pick one, say how often it may fire, say what to post,
 * check it against history, and switch it on -- delivering to the Demo inbox,
 * so there is nothing to sign up for first.
 *
 * Everything goes through the same endpoints the Rules page uses. The rule it
 * makes is an ordinary rule, editable there afterwards.
 */

const INBOX_NAME = "Demo inbox";
const BACKTEST_DAYS = 90;
// While the last step is on screen, ask for a poll this often rather than
// waiting out the trigger's own interval (a minute by default). Only while
// someone is watching, and only the one trigger.
const WATCH_POLL_MS = 10_000;
const WATCH_REFRESH_MS = 3000;

type Cadence = "day" | "week" | "first";
type Shape = "message" | "ticket";

const CADENCES: { id: Cadence; label: (key: string) => string }[] = [
  { id: "day", label: (key) => `At most once a day per ${key}` },
  { id: "week", label: (key) => `At most once a week per ${key}` },
  { id: "first", label: (key) => `Only the first time, per ${key}` },
];

/** `account_id` -> `account`: how a person would say the grain. */
function grain(key: string[]): string {
  if (key.length === 0) return "row";
  return key
    .map((column) => column.replace(/_(id|key|uuid)$/i, "").replace(/_/g, " "))
    .join(" and ");
}

function humanise(name: string): string {
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Substitute `{{ column }}` from a row, the way the dispatcher will. */
function fill(template: string, row: Record<string, unknown>): string {
  return template.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (whole, name: string) => {
    const value = name
      .split(".")
      .reduce<unknown>(
        (at, part) =>
          at && typeof at === "object" ? (at as Record<string, unknown>)[part] : undefined,
        row,
      );
    if (value === undefined) return whole;
    return value === null ? "" : String(value);
  });
}

/** Columns worth putting in a first message: not the clock, not bare ids. */
function telling(columns: string[], trigger: TriggerInfo): string[] {
  const skip = new Set([trigger.event_time ?? "", ...trigger.key]);
  const picked = columns.filter(
    (c) => !skip.has(c) && !/(^id$|_id$|_at$|^created|^updated)/i.test(c),
  );
  return (picked.length ? picked : columns.filter((c) => !skip.has(c))).slice(0, 3);
}

function defaultMessage(trigger: TriggerInfo, columns: string[]): string {
  const what = trigger.description ?? humanise(trigger.name);
  const parts = telling(columns, trigger).map((c) => `${c.replace(/_/g, " ")}: *{{ ${c} }}*`);
  return `:bell: ${what}${parts.length ? ` — ${parts.join(", ")}` : ""}`;
}

function defaultTicket(trigger: TriggerInfo, columns: string[], row: Record<string, unknown>) {
  const what = trigger.description ?? humanise(trigger.name);
  const shown = telling(columns, trigger);
  // By value as well as by name: the column holding an address is not always
  // called email (`respondent`, `owner`, `contact`).
  const looksLikeEmail = (c: string) => /^[^\s@]+@[^\s@]+$/.test(String(row[c] ?? ""));
  const email = columns.find((c) => /email/i.test(c)) ?? columns.find(looksLikeEmail);
  const name = columns.find((c) => /(^|_)name$/i.test(c) && c !== shown[0]);
  return {
    subject: shown[0] ? `${what}: {{ ${shown[0]} }}` : what,
    body: columns
      .filter((c) => c !== trigger.event_time && !/(^id$|_id$)/i.test(c))
      .slice(0, 6)
      .map((c) => `${c.replace(/_/g, " ")}: {{ ${c} }}`)
      .join("\n"),
    requester_email: email ? `{{ ${email} }}` : "support@example.com",
    requester_name: name ? `{{ ${name} }}` : "",
    priority: "normal",
  };
}

function uniqueName(base: string, taken: string[]): string {
  if (!taken.includes(base)) return base;
  for (let i = 2; ; i++) if (!taken.includes(`${base}_${i}`)) return `${base}_${i}`;
}

function errorText(exc: unknown): string {
  if (exc instanceof ApiError) {
    return exc.errors?.length ? `${exc.message}: ${exc.errors.join("; ")}` : exc.message;
  }
  return String(exc);
}

/* ------------------------------------------------------------------ steps */

function Step({
  n,
  title,
  done,
  active,
  summary,
  onEdit,
  children,
}: {
  n: number;
  title: string;
  done: boolean;
  active: boolean;
  summary?: React.ReactNode;
  onEdit?: () => void;
  children?: React.ReactNode;
}) {
  return (
    <section className={`card step${active ? " step-active" : ""}${done ? " step-done" : ""}`}>
      <div className="step-head">
        <span className="step-n" aria-hidden="true">
          {done ? "✓" : n}
        </span>
        <h2>{title}</h2>
        <span className="spacer" />
        {done && !active && onEdit && (
          <button className="btn" onClick={onEdit}>
            Change
          </button>
        )}
      </div>
      {done && !active && summary && <div className="step-summary">{summary}</div>}
      {active && <div className="step-body">{children}</div>}
    </section>
  );
}

export function Start({ onConnected }: { onConnected: (has: boolean) => void }) {
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // 1. connect
  const [sources, setSources] = useState<SourceInfo[] | null>(null);
  const [suggested, setSuggested] = useState<SuggestedSource[]>([]);
  const [canSimulate, setCanSimulate] = useState(false);
  const [hosted, setHosted] = useState(false);
  // 2. event
  const [triggers, setTriggers] = useState<TriggerInfo[]>([]);
  const [ruleNames, setRuleNames] = useState<string[]>([]);
  const [trigger, setTrigger] = useState<TriggerInfo | null>(null);
  // 3. cadence
  const [cadence, setCadence] = useState<Cadence>("day");
  const [ruleName, setRuleName] = useState<string | null>(null);
  // The draft this page saved, if any. Saving one for a different event deletes it,
  // so trying a few leaves one rule behind rather than several.
  const [saved, setSaved] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  // 4. destination
  const [shape, setShape] = useState<Shape>("message");
  const [channel, setChannel] = useState("#alerts");
  const [message, setMessage] = useState("");
  const [ticket, setTicket] = useState(() => ({
    subject: "",
    body: "",
    requester_email: "",
    requester_name: "",
    priority: "normal",
  }));
  // 6. live
  const [live, setLive] = useState(false);
  const [arrived, setArrived] = useState<InboxItem[]>([]);
  // Rules already delivering to the Demo inbox, from an earlier visit to this
  // page: after a reload the steps start over, and these say what is running.
  const [running, setRunning] = useState<string[]>([]);

  const load = useCallback(async () => {
    try {
      const [listed, health] = await Promise.all([api.sources(), api.health()]);
      setSources(listed.sources);
      setSuggested(health.suggested_sources ?? []);
      setCanSimulate(Boolean(health.demo_activity));
      setHosted(Boolean(health.hosted));
      if (listed.sources.length > 0) {
        const defs = await api.definitions().catch(() => null);
        setTriggers(defs?.triggers ?? []);
        setRuleNames(defs?.rules.map((r) => r.name) ?? []);
        const [states, bound] = await Promise.all([
          api.rules().catch(() => null),
          api.bindings().catch(() => null),
        ]);
        const toInbox = new Set(
          (bound?.bindings ?? [])
            .filter((b) => b.integration === INBOX_NAME && b.enabled)
            .map((b) => b.rule_name),
        );
        setRunning(
          (states?.rules ?? [])
            .filter((r) => r.mode === "live" && r.enabled && toInbox.has(r.name))
            .map((r) => r.name),
        );
      }
      setStep((current) => (current === 0 ? (listed.sources.length > 0 ? 2 : 1) : current));
    } catch (exc) {
      setError(errorText(exc));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const connected = new Set((sources ?? []).map((s) => s.name));
  const defaultSource = sources?.find((s) => s.default)?.name ?? null;
  const readable = (t: TriggerInfo) => connected.has(t.source ?? defaultSource ?? "");

  /* ---------------------------------------------------------- actions */

  const connectDemo = async () => {
    setBusy("connect");
    setError(null);
    try {
      for (const demo of suggested) {
        if (!connected.has(demo.name)) await api.addSource(demo.name, demo.dsn);
      }
      onConnected(true);
      setStep(2);
      await load();
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(null);
    }
  };

  // Saving and backtesting happen together: the backtest runs a saved rule,
  // and a new rule is saved in shadow, where it cannot send anything.
  const saveAndBacktest = async (chosen: TriggerInfo, how: Cadence, name: string) => {
    setBusy("backtest");
    setError(null);
    try {
      // A draft saved for a different event goes first, and is awaited:
      // each write replaces the whole definitions document, so two in flight
      // at once can undo each other.
      if (saved && saved !== name) {
        await api.deleteRule(saved).catch(() => undefined);
        setRuleNames((names) => names.filter((n) => n !== saved));
        setSaved(null);
      }
      await api.upsertRule(name, {
        name,
        trigger: chosen.name,
        description: `${CADENCES.find((c) => c.id === how)?.label(grain(chosen.key))}. Made in Get started.`,
        policy: how === "first" ? "once_ever" : "once_per_period",
        period: how === "first" ? null : how,
        n: null,
      });
      setSaved(name);
      setRuleNames((names) => (names.includes(name) ? names : [...names, name]));
      const ran = await api.backtest(name, BACKTEST_DAYS, 5);
      setResult(ran);
      if (!message) setMessage(defaultMessage(chosen, ran.columns));
      if (!ticket.subject) setTicket(defaultTicket(chosen, ran.columns, ran.sample[0] ?? {}));
      setStep(4);
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(null);
    }
  };

  const parameters = (): [string, Record<string, string>] =>
    shape === "message"
      ? ["post_message", { channel, text: message }]
      : [
          "create_ticket",
          Object.fromEntries(Object.entries(ticket).filter(([, v]) => v !== "")) as Record<
            string,
            string
          >,
        ];

  const turnOn = async () => {
    if (!trigger || !ruleName) return;
    setBusy("live");
    setError(null);
    try {
      const listed = await api.integrations();
      if (!listed.integrations.some((i) => i.name === INBOX_NAME)) {
        await api.saveIntegration({
          name: INBOX_NAME,
          description: null,
          provider: "demo_inbox",
          base_url: "",
          auth_kind: "none",
          auth_header_name: null,
          auth_credential: "token",
          credentials: {},
        });
      }
      const [action, values] = parameters();
      // Capped per key, so "once a day per account" means per account rather
      // than one shared allowance for the whole inbox.
      const recipient = trigger.key.map((k) => `{{ ${k} }}`).join(":");
      await api.createBinding(ruleName, INBOX_NAME, action, values, recipient || null);
      await api.setMode(ruleName, "live");
      await api.runTrigger(trigger.name);
      setLive(true);
      setStep(6);
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(null);
    }
  };

  // Watching: poll this trigger sooner than its schedule, and show what lands.
  const triggerName = trigger?.name;
  const timer = useRef<number | null>(null);
  useEffect(() => {
    if (!live || !triggerName || !ruleName) return;
    const refresh = async () => {
      try {
        const inbox = await api.inbox();
        // The rule is new, so everything it has delivered arrived after it
        // went live -- no clock comparison between browser and server needed.
        const mine = inbox.items.filter((item) => item.rule === ruleName && item.status === "sent");
        setArrived(mine);
        if (mine.length > 0) moment("first_delivery");
      } catch {
        // The next tick will try again; a blip is not worth a banner.
      }
    };
    void refresh();
    const view = window.setInterval(() => void refresh(), WATCH_REFRESH_MS);
    timer.current = window.setInterval(
      () => void api.runTrigger(triggerName).catch(() => undefined),
      WATCH_POLL_MS,
    );
    return () => {
      window.clearInterval(view);
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [live, triggerName, ruleName]);

  /* ---------------------------------------------------------- previews */

  const previews = useMemo<InboxItem[]>(() => {
    const rows = result?.sample ?? [];
    return rows.slice(0, 3).map((row, i) => ({
      id: `preview-${i}`,
      rule: ruleName ?? "",
      integration: INBOX_NAME,
      mode: "shadow",
      status: "suppressed",
      reason: null,
      kind: shape,
      body:
        shape === "message"
          ? { channel, text: fill(message, row) }
          : {
              ticket: {
                subject: fill(ticket.subject, row),
                comment: { body: fill(ticket.body, row) },
                requester: {
                  name: fill(ticket.requester_name, row),
                  email: fill(ticket.requester_email, row),
                },
                priority: ticket.priority,
              },
            },
      created_at: "",
      sent_at: null,
    }));
  }, [result, shape, channel, message, ticket, ruleName]);

  const columns = result?.columns ?? [];
  const insert = (column: string) => {
    const token = `{{ ${column} }}`;
    if (shape === "message") setMessage((m) => `${m}${m.endsWith(" ") || !m ? "" : " "}${token}`);
    else
      setTicket((t) => ({
        ...t,
        body: `${t.body}${t.body ? "\n" : ""}${column.replace(/_/g, " ")}: ${token}`,
      }));
  };

  /* ---------------------------------------------------------- render */

  if (!sources) {
    return (
      <div className="card">
        <p className="hint">{error ?? "Loading…"}</p>
      </div>
    );
  }

  const key = trigger ? grain(trigger.key) : "row";
  const cadenceLabel = CADENCES.find((c) => c.id === cadence)?.label(key) ?? "";

  return (
    <div className="start">
      <p className="lede">
        When something happens in the database, post about it — no code. It delivers to the{" "}
        <Link to={href("inbox")} className="ref">
          {INBOX_NAME}
        </Link>
        , so nothing is sent anywhere.
      </p>

      {error && <div className="notice bad">{error}</div>}

      {!live && running.length > 0 && (
        <div className="notice">
          <p style={{ margin: "0 0 8px" }}>
            Already live and posting to the{" "}
            <Link to={href("inbox")} className="ref">
              {INBOX_NAME}
            </Link>
            : {running.map((name, i) => (
              <span key={name}>
                {i > 0 && ", "}
                <code>{name}</code>
              </span>
            ))}
            . Make new activity to see them fire, or set up another below.
          </p>
          {canSimulate && <SimulateButton />}
        </div>
      )}

      <Step
        n={1}
        title="Connect a database"
        active={step === 1}
        done={sources.length > 0}
        summary={
          <span className="row" style={{ gap: 8 }}>
            {sources.map((s) => (
              <span key={s.name} className="with-icon" style={{ gap: 6 }}>
                <BrandIcon name={s.kind} size="sm" label="" />
                {s.name}
              </span>
            ))}
          </span>
        }
      >
        <p className="hint">
          Rowfire reads a database and never writes to it. Start with the sample one: a small SaaS
          company with accounts, billing, trials and an NPS survey.
        </p>
        {suggested.length > 0 ? (
          <button
            className="btn primary"
            onClick={() => void connectDemo()}
            disabled={busy !== null}
          >
            {busy === "connect" ? "Connecting…" : "Use the sample database"}
          </button>
        ) : (
          <p className="hint">No sample database is configured here.</p>
        )}
        {!hosted && (
          <Link to={href("sources")} className="ref" style={{ marginLeft: 14 }}>
            or connect your own
          </Link>
        )}
      </Step>

      <Step
        n={2}
        title="Pick an event"
        active={step === 2}
        done={step > 2 && trigger !== null}
        onEdit={live ? undefined : () => setStep(2)}
        summary={
          trigger && (
            <>
              <strong>{trigger.description ?? humanise(trigger.name)}</strong>
              <details className="sql-peek">
                <summary>The SQL behind it, written once by whoever knows the schema</summary>
                <pre className="sql">{trigger.sql}</pre>
              </details>
            </>
          )
        }
      >
        <p className="hint">
          Events are defined once, as SQL, by whoever knows the schema. Pick the one you want to act
          on.
        </p>
        {triggers.length === 0 ? (
          <p className="hint">
            No events are defined yet. Somebody who knows the database writes them under{" "}
            <Link to={href("triggers")} className="ref">
              Triggers
            </Link>
            .
          </p>
        ) : (
          <div className="event-grid">
            {triggers.map((t) => {
              const ok = readable(t);
              return (
                <button
                  key={t.name}
                  className={`event-card${trigger?.name === t.name ? " on" : ""}`}
                  disabled={!ok || busy !== null}
                  title={ok ? undefined : `Reads \`${t.source}\`, which is not connected`}
                  onClick={() => {
                    setTrigger(t);
                    setResult(null);
                    setMessage("");
                    setTicket((current) => ({ ...current, subject: "" }));
                    setRuleName(
                      uniqueName(
                        `${t.name}_alert`,
                        ruleNames.filter((n) => n !== saved),
                      ),
                    );
                    setStep(3);
                  }}
                >
                  <strong>{t.description ?? humanise(t.name)}</strong>
                  <span className="muted">
                    {t.name} · one per {grain(t.key)}
                    {ok ? "" : ` · needs ${t.source}`}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </Step>

      <Step
        n={3}
        title="Decide how often"
        active={step === 3}
        done={step > 3 && result !== null}
        onEdit={live ? undefined : () => setStep(3)}
        summary={cadenceLabel}
      >
        <p className="hint">
          The same {key} can trigger this many times. Say how often it should reach you.
        </p>
        <div className="choice-list" role="radiogroup" aria-label="How often">
          {CADENCES.map((c) => (
            <label key={c.id} className={`choice${cadence === c.id ? " on" : ""}`}>
              <input
                type="radio"
                name="cadence"
                checked={cadence === c.id}
                onChange={() => setCadence(c.id)}
              />
              {c.label(key)}
            </label>
          ))}
        </div>
        <button
          className="btn primary"
          disabled={!trigger || !ruleName || busy !== null}
          onClick={() => trigger && ruleName && void saveAndBacktest(trigger, cadence, ruleName)}
        >
          {busy === "backtest" ? "Checking history…" : "Continue"}
        </button>
      </Step>

      <Step
        n={4}
        title="Say what to post"
        active={step === 4}
        done={step > 4}
        onEdit={live ? undefined : () => setStep(4)}
        summary={
          <span className="with-icon" style={{ gap: 8 }}>
            <BrandIcon name="demo_inbox" size="sm" label="" />
            {shape === "message" ? `A message in ${channel}` : "A support ticket"}, in the{" "}
            {INBOX_NAME}
          </span>
        }
      >
        <div className="row" style={{ gap: 12, alignItems: "center", marginBottom: 12 }}>
          <div className="segmented" role="group" aria-label="What to post">
            <button
              className={shape === "message" ? "on" : ""}
              aria-pressed={shape === "message"}
              onClick={() => setShape("message")}
            >
              Chat message
            </button>
            <button
              className={shape === "ticket" ? "on" : ""}
              aria-pressed={shape === "ticket"}
              onClick={() => setShape("ticket")}
            >
              Support ticket
            </button>
          </div>
          <span className="muted" style={{ fontSize: 13 }}>
            shaped like Slack and Zendesk; connect the real ones later under{" "}
            <Link to={href("integrations")} className="ref">
              Integrations
            </Link>
          </span>
        </div>

        <div className="compose">
          <div className="compose-form">
            {shape === "message" ? (
              <>
                <label className="field">
                  <span>Channel</span>
                  <input type="text" value={channel} onChange={(e) => setChannel(e.target.value)} />
                </label>
                <label className="field">
                  <span>Message</span>
                  <textarea rows={4} value={message} onChange={(e) => setMessage(e.target.value)} />
                </label>
              </>
            ) : (
              <>
                <label className="field">
                  <span>Subject</span>
                  <input
                    type="text"
                    value={ticket.subject}
                    onChange={(e) => setTicket({ ...ticket, subject: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Description</span>
                  <textarea
                    rows={5}
                    value={ticket.body}
                    onChange={(e) => setTicket({ ...ticket, body: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Requester email</span>
                  <input
                    type="text"
                    value={ticket.requester_email}
                    onChange={(e) => setTicket({ ...ticket, requester_email: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Priority</span>
                  <select
                    value={ticket.priority}
                    onChange={(e) => setTicket({ ...ticket, priority: e.target.value })}
                  >
                    {["low", "normal", "high", "urgent"].map((p) => (
                      <option key={p}>{p}</option>
                    ))}
                  </select>
                </label>
              </>
            )}
            {columns.length > 0 && (
              <div className="chips" aria-label="Insert a column">
                <span className="muted" style={{ fontSize: 12.5 }}>
                  Insert:
                </span>
                {columns.map((c) => (
                  <button key={c} className="chip" onClick={() => insert(c)}>
                    {c}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="compose-preview">
            <div className="muted" style={{ fontSize: 12.5, marginBottom: 6 }}>
              Preview, from real rows
            </div>
            {previews.length === 0 ? (
              <p className="hint">Nothing in the last {BACKTEST_DAYS} days to preview with.</p>
            ) : (
              <InboxEntry item={previews[0]} />
            )}
          </div>
        </div>

        <button
          className="btn primary"
          style={{ marginTop: 16 }}
          disabled={
            shape === "message"
              ? !message.trim()
              : !ticket.subject.trim() || !ticket.requester_email.trim()
          }
          onClick={() => setStep(5)}
        >
          Continue
        </button>
      </Step>

      <Step
        n={5}
        title="Check it against history"
        active={step === 5}
        done={step > 5}
        summary={
          result && (
            <span>
              Would have posted <strong>{result.fires}</strong> times in the last {BACKTEST_DAYS}{" "}
              days
            </span>
          )
        }
      >
        {result && (
          <>
            <div className="stat-row">
              <div>
                <div className="stat-big">{result.fires}</div>
                <div className="muted">
                  {shape === "message" ? "messages" : "tickets"} in the last {BACKTEST_DAYS} days
                </div>
              </div>
              <div>
                <div className="stat-big">{result.matched_rows}</div>
                <div className="muted">times it happened</div>
              </div>
              <div>
                <div className="stat-big">{result.dedup_removed}</div>
                <div className="muted">repeats held back</div>
              </div>
            </div>
            <p className="hint">
              If this rule had been running, {cadenceLabel.toLowerCase()} would have meant{" "}
              {result.fires} {shape === "message" ? "messages" : "tickets"}. Nothing was sent to
              work that out. A few of them:
            </p>
            <div className="inbox-list">
              {previews.map((item) => (
                <InboxEntry key={item.id} item={item} />
              ))}
            </div>
          </>
        )}
        <div className="row" style={{ gap: 12, marginTop: 16 }}>
          <button className="btn primary" disabled={busy !== null} onClick={() => void turnOn()}>
            {busy === "live" ? "Turning it on…" : "Turn it on"}
          </button>
          <span className="muted" style={{ fontSize: 13 }}>
            From now on only — history never fires.
          </span>
        </div>
      </Step>

      <Step n={6} title="Watch it work" active={step === 6} done={arrived.length > 0}>
        <div className="notice good">
          <div className="notice-head">
            <span aria-hidden="true">●</span> <code>{ruleName}</code> is live
          </div>
          It fires on new rows from now on.{" "}
          {canSimulate
            ? "Add some activity to the sample database"
            : "Add some activity to the database"}{" "}
          and its {shape === "message" ? "messages" : "tickets"} appear below within a few seconds.
        </div>
        {canSimulate && (
          <div style={{ margin: "14px 0" }}>
            <SimulateButton primary={arrived.length === 0} />
          </div>
        )}
        {arrived.length === 0 ? (
          <p className="hint waiting">
            <span className="pulse" aria-hidden="true" /> Waiting for new activity… checking every{" "}
            {WATCH_POLL_MS / 1000} seconds while this page is open.
          </p>
        ) : (
          <div className="inbox-list" aria-live="polite">
            {arrived.map((item) => (
              <InboxEntry key={item.id} item={item} />
            ))}
          </div>
        )}
        <p className="footnote" style={{ marginBottom: 0 }}>
          Everything it delivers is in the{" "}
          <Link to={href("inbox")} className="ref">
            {INBOX_NAME}
          </Link>
          . The rule is an ordinary one: change it under{" "}
          <Link to={href("rules", ruleName)} className="ref">
            Rules
          </Link>
          , pause it under{" "}
          <Link to={href("activity")} className="ref">
            Activity
          </Link>
          , or{" "}
          <button className="linklike" onClick={() => window.location.assign(href("start"))}>
            set up another
          </button>
          .
        </p>
      </Step>
    </div>
  );
}
