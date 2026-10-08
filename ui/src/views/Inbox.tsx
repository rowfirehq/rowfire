import { useCallback, useEffect, useState, type ReactNode } from "react";

import { ApiError, api, type Inbox as InboxData, type InboxItem } from "../api";
import { BrandIcon } from "../components/BrandIcon";
import { Link } from "../components/Link";
import { SimulateButton } from "../components/SimulateButton";
import { href } from "../router";

/* The Demo inbox: where a rule's messages land when nothing real is connected.
 *
 * A request to `inbox://` is delivered by being recorded, so this page is the
 * delivery log, drawn the way the destination would draw it -- a chat message
 * or a ticket -- instead of as JSON. Shadow deliveries are shown too and
 * labelled: what a rule *would* post is the thing worth seeing before
 * promoting it.
 */

const REFRESH_MS = 3000;

const INBOX_NAME = "Demo inbox";

type Filter = "all" | "delivered" | "shadow";

function ago(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

// The handful of Slack shortcodes the examples use, so a message written for
// Slack reads the same here. Anything else is left as typed.
const EMOJI: Record<string, string> = {
  tada: "🎉",
  warning: "⚠️",
  rotating_light: "🚨",
  speech_balloon: "💬",
  white_check_mark: "✅",
  x: "❌",
  fire: "🔥",
  wave: "👋",
  bell: "🔔",
  moneybag: "💰",
  rocket: "🚀",
  eyes: "👀",
};

/**
 * Slack-style text: *bold*, _italic_, `code` and :shortcodes:, built as React
 * nodes. Never HTML -- these values come straight from database rows.
 */
function formatted(text: string): ReactNode[] {
  const pattern = /(\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`|:[a-z0-9_+-]+:)/g;
  const out: ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(pattern)) {
    const token = match[0];
    const at = match.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const inner = token.slice(1, -1);
    const key = `t${index++}`;
    if (token.startsWith("*")) out.push(<strong key={key}>{inner}</strong>);
    else if (token.startsWith("_")) out.push(<em key={key}>{inner}</em>);
    else if (token.startsWith("`")) out.push(<code key={key}>{inner}</code>);
    else out.push(EMOJI[inner] ?? token);
    last = at + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function Status({ item }: { item: InboxItem }) {
  if (item.status === "sent") return <span className="tag live">delivered</span>;
  if (item.status === "failed")
    return (
      <span className="tag bad-tag" title={item.reason ?? undefined}>
        failed
      </span>
    );
  if (item.mode === "shadow") return <span className="tag">shadow · not sent</span>;
  return (
    <span className="tag" title={item.reason ?? undefined}>
      held back
    </span>
  );
}

function Meta({ item }: { item: InboxItem }) {
  // A preview is built in the browser and was never delivered: no status,
  // no rule, no time -- just what the message would say.
  if (item.id.startsWith("preview")) return null;
  return (
    <div className="inbox-meta">
      <Status item={item} />
      <span className="muted">
        from rule{" "}
        <Link to={href("rules", item.rule)} className="ref">
          {item.rule}
        </Link>
      </span>
      <span className="spacer" />
      <time className="muted" dateTime={item.created_at} title={item.created_at}>
        {ago(item.created_at)}
      </time>
    </div>
  );
}

function Message({ item }: { item: InboxItem }) {
  const body = record(item.body);
  return (
    <article className="inbox-item inbox-message">
      <div className="inbox-avatar" aria-hidden="true">
        R
      </div>
      <div className="inbox-content">
        <div className="inbox-head">
          <strong>Rowfire</strong>
          <span className="inbox-app">APP</span>
          <span className="inbox-channel">{text(body.channel) || "#general"}</span>
        </div>
        <div className="inbox-text">{formatted(text(body.text))}</div>
        <Meta item={item} />
      </div>
    </article>
  );
}

function Ticket({ item }: { item: InboxItem }) {
  const ticket = record(record(item.body).ticket);
  const requester = record(ticket.requester);
  const priority = text(ticket.priority) || "normal";
  const email = text(requester.email);
  // Rules often fill both from one column; saying it twice reads as a bug.
  const who = text(requester.name) === email ? "" : text(requester.name);
  return (
    <article className="inbox-item inbox-ticket">
      <div className="inbox-content">
        <div className="inbox-head">
          <span className={`priority priority-${priority}`}>{priority}</span>
          <strong className="inbox-subject">{text(ticket.subject) || "(no subject)"}</strong>
        </div>
        {(who || email) && (
          <div className="muted inbox-requester">
            {who}
            {who && email ? " · " : ""}
            {email}
          </div>
        )}
        <div className="inbox-text inbox-ticket-body">{text(record(ticket.comment).body)}</div>
        <Meta item={item} />
      </div>
    </article>
  );
}

function Other({ item }: { item: InboxItem }) {
  return (
    <article className="inbox-item">
      <div className="inbox-content">
        {item.status === "failed" && item.reason ? (
          <div className="bad-text inbox-text">{item.reason}</div>
        ) : (
          <pre className="sql">{JSON.stringify(item.body, null, 2)}</pre>
        )}
        <Meta item={item} />
      </div>
    </article>
  );
}

/** One inbox item, drawn the way its destination would draw it. */
export function InboxEntry({ item }: { item: InboxItem }) {
  if (item.kind === "message") return <Message item={item} />;
  if (item.kind === "ticket") return <Ticket item={item} />;
  return <Other item={item} />;
}

export function Inbox() {
  const [data, setData] = useState<InboxData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [adding, setAdding] = useState(false);
  const [canSimulate, setCanSimulate] = useState(false);

  useEffect(() => {
    api
      .health()
      .then((health) => setCanSimulate(Boolean(health.demo_activity)))
      .catch(() => undefined);
  }, []);

  const load = useCallback(async () => {
    try {
      setData(await api.inbox());
      setError(null);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const add = async () => {
    setAdding(true);
    try {
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
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setAdding(false);
    }
  };

  if (!data) {
    return (
      <div className="card">
        <p className="hint">{error ?? "Loading…"}</p>
      </div>
    );
  }

  const items = data.items.filter((item) =>
    filter === "all"
      ? true
      : filter === "delivered"
        ? item.status === "sent"
        : item.mode === "shadow",
  );
  const delivered = data.items.filter((item) => item.status === "sent").length;

  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div className="with-icon" style={{ alignItems: "flex-start", gap: 12 }}>
            <BrandIcon name="demo_inbox" size="lg" label="" />
            <div>
              <h2>Try a rule without connecting anything</h2>
              <p className="hint" style={{ marginBottom: 0 }}>
                Point a rule's action at the <strong>{INBOX_NAME}</strong> and its messages and
                tickets land here instead of in Slack or Zendesk. Nothing leaves this machine.
                Shadow-mode rules show what they would post; promote one to live under{" "}
                <Link to={href("activity")} className="ref">
                  Activity
                </Link>{" "}
                to see it delivered.
              </p>
            </div>
          </div>
          {data.installed.length === 0 && (
            <button className="btn primary" onClick={() => void add()} disabled={adding}>
              {adding ? "Adding…" : "Add the Demo inbox"}
            </button>
          )}
        </div>
        {data.installed.length > 0 && (
          <p className="footnote" style={{ marginTop: 12, marginBottom: 0 }}>
            Delivers here:{" "}
            {data.installed.map((name, i) => (
              <span key={name}>
                {i > 0 && ", "}
                <Link to={href("integrations", name)} className="ref">
                  {name}
                </Link>
              </span>
            ))}
            . Attach one of its actions to a rule on the{" "}
            <Link to={href("rules")} className="ref">
              Rules
            </Link>{" "}
            page.
          </p>
        )}
        {canSimulate && data.installed.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <SimulateButton onDone={() => void load()} />
          </div>
        )}
        {error && <div className="notice bad">{error}</div>}
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>
            {data.items.length === 0
              ? "Nothing yet"
              : `${data.items.length} item${data.items.length === 1 ? "" : "s"}, ${delivered} delivered`}
          </h2>
          <div className="segmented" role="group" aria-label="Show">
            {(["all", "delivered", "shadow"] as Filter[]).map((option) => (
              <button
                key={option}
                className={filter === option ? "on" : ""}
                aria-pressed={filter === option}
                onClick={() => setFilter(option)}
              >
                {option}
              </button>
            ))}
          </div>
        </div>

        {data.items.length === 0 ? (
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            When a rule with a {INBOX_NAME} action fires, it shows up here within a few seconds.
            Rules only fire on rows newer than when they started, so add some activity to the
            database to see one arrive.
          </p>
        ) : items.length === 0 ? (
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            Nothing {filter === "delivered" ? "delivered" : "in shadow"} yet.
          </p>
        ) : (
          <div className="inbox-list" aria-live="polite">
            {items.map((item) => (
              <InboxEntry key={item.id} item={item} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}
