import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, api, type AppEvent, type AppEventKey } from "../api";
import { BrandIcon } from "./BrandIcon";
import { suggestHandled } from "../moments";
import { href, navigate } from "../router";

/* An app connection: the app sends events itself, instead of Rowfire reading
 * a database. Shown on the Connect page for a `rowfire://events` connection.
 *
 * First where the app runs, then only that platform's steps. Every platform
 * needs the same two things from here -- this server's address and an API key
 * -- and ends the same way, with the first event arriving; what differs is
 * how the two get into the app. So the key and the wait are shared, and a
 * platform supplies only its own steps in between.
 *
 * The platform is in the URL (/connect/<connection>/python), so a reload, or a
 * link sent to whoever will do the install, opens on the same steps.
 */

const REFRESH_MS = 3000;

interface Platform {
  id: string;
  name: string;
  blurb: string;
  icon: string;
}

const PLATFORMS: Platform[] = [
  {
    id: "python",
    name: "Python + SQLAlchemy",
    blurb: "FastAPI, Flask, Starlette, scripts and jobs",
    icon: "python",
  },
  {
    id: "frappe",
    name: "Frappe / ERPNext",
    blurb: "An app from the Frappe Cloud Marketplace",
    icon: "frappe",
  },
];

const INSTALL =
  'pip install "rowfire-sdk[sqlalchemy] @ git+https://github.com/rowfirehq/rowfire-sdk-python"';

function ago(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function pythonSnippet(endpoint: string, key: string | null): string {
  const apiKey = key ? `"${key}"` : 'os.environ["ROWFIRE_API_KEY"]';
  return [
    "import os",
    "",
    "import rowfire_sdk as rowfire",
    "from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy",
    "from rowfire_sdk.integrations.asgi import RowfireMiddleware",
    "",
    "rowfire.init(",
    `    endpoint="${endpoint}",`,
    `    api_key=${apiKey},`,
    ")",
    "rowfire_sqlalchemy.instrument()          # every SQLAlchemy session",
    "app.add_middleware(RowfireMiddleware)    # FastAPI / Starlette",
  ].join("\n");
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn"
      onClick={() => {
        navigator.clipboard
          ?.writeText(text)
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          })
          .catch(() => undefined);
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function Code({ text }: { text: string }) {
  return (
    <div className="code-block">
      <pre>{text}</pre>
      <CopyButton text={text} />
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <section className="connect-step">
      <span className="step-n">{n}</span>
      <div className="connect-step-body">
        <h3>{title}</h3>
        {children}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ keys

function useKeys() {
  const [keys, setKeys] = useState<AppEventKey[] | null>(null);
  const [fresh, setFresh] = useState<{ prefix: string; key: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .appEventKeys()
      .then((answer) => setKeys(answer.keys.filter((key) => !key.revoked)))
      .catch((exc) => setError(exc instanceof ApiError ? exc.message : String(exc)));
  }, []);
  useEffect(load, [load]);

  const create = async (name: string) => {
    setError(null);
    try {
      const made = await api.createAppEventKey(name);
      setFresh({ prefix: made.prefix, key: made.key });
      load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  const revoke = async (prefix: string) => {
    if (!window.confirm(`Revoke ${prefix}…? Apps sending with it will be refused.`)) return;
    try {
      await api.revokeAppEventKey(prefix);
      setFresh((current) => (current?.prefix === prefix ? null : current));
      load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  return { keys, fresh, error, create, revoke };
}

function KeyStep({
  n,
  platform,
  keys,
}: {
  n: number;
  platform: Platform;
  keys: ReturnType<typeof useKeys>;
}) {
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    await keys.create(platform.id);
    setBusy(false);
  };
  const existing = keys.keys ?? [];

  return (
    <Step n={n} title="Create an API key">
      {keys.fresh ? (
        <div className="notice good">
          <div className="notice-head">Copy it now — it won't be shown again</div>
          <Code text={keys.fresh.key} />
        </div>
      ) : (
        <form onSubmit={submit}>
          <p className="hint">The key is shown once, right after you create it.</p>
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create key"}
          </button>
        </form>
      )}
      {keys.error && (
        <p className="bad-text" role="alert">
          {keys.error}
        </p>
      )}
      {existing.length > 0 && (
        <details className="manage-keys">
          <summary>Manage keys ({existing.length})</summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Key</th>
                  <th>Created</th>
                  <th>Last used</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {existing.map((key) => (
                  <tr key={key.prefix}>
                    <td>{key.name}</td>
                    <td className="mono">{key.prefix}…</td>
                    <td>{ago(key.created_at)}</td>
                    <td>{ago(key.last_used_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="linklike" onClick={() => keys.revoke(key.prefix)}>
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </Step>
  );
}

// ------------------------------------------------------------- platforms

function PythonSteps({
  endpoint,
  keys,
  platform,
}: {
  endpoint: string;
  keys: ReturnType<typeof useKeys>;
  platform: Platform;
}) {
  return (
    <>
      <Step n={1} title="Install the SDK">
        <Code text={INSTALL} />
      </Step>
      <KeyStep n={2} platform={platform} keys={keys} />
      <Step n={3} title="Start it when your app starts">
        <Code text={pythonSnippet(endpoint, keys.fresh?.key ?? null)} />
        <p className="footnote">
          {keys.fresh
            ? "Your new key is filled in — move it to ROWFIRE_API_KEY before you commit this. "
            : ""}
          Jobs name themselves with <code>with rowfire.operation("job NightlyBilling"):</code>
        </p>
      </Step>
    </>
  );
}

function FrappeSteps({
  endpoint,
  keys,
  platform,
  appUrl,
}: {
  endpoint: string;
  keys: ReturnType<typeof useKeys>;
  platform: Platform;
  appUrl: string | null;
}) {
  return (
    <>
      <Step n={1} title="Install the Rowfire app on your site">
        {appUrl ? (
          <p className="hint">
            From the Frappe Cloud Marketplace:{" "}
            <a href={appUrl} target="_blank" rel="noreferrer">
              Rowfire for Frappe
            </a>
            .
          </p>
        ) : (
          <p className="hint">
            The Marketplace listing is coming soon. You can create your key now and keep it
            for when it's there.
          </p>
        )}
      </Step>
      <KeyStep n={2} platform={platform} keys={keys} />
      <Step n={3} title="Paste both into the app's settings">
        <p className="hint">In your Frappe site, open the Rowfire app's settings:</p>
        <label className="field">Rowfire address</label>
        <Code text={endpoint} />
        <label className="field" style={{ marginTop: 12 }}>
          API key
        </label>
        {keys.fresh ? (
          <Code text={keys.fresh.key} />
        ) : (
          <p className="muted">The key from step 2.</p>
        )}
      </Step>
    </>
  );
}

// ------------------------------------------------------------------ wait

function useEvents() {
  const [events, setEvents] = useState<{ total: number; events: AppEvent[] } | null>(null);
  const [lost, setLost] = useState(0);
  // Waiting for the first event is the point of the page, so it keeps asking.
  useEffect(() => {
    let live = true;
    const tick = () => {
      api
        .appEventsRecent(10)
        .then((answer) => live && setEvents(answer))
        .catch(() => undefined);
      api
        .appEventLosses()
        .then(
          (answer) => live && setLost(answer.losses.reduce((sum, row) => sum + row.count, 0)),
        )
        .catch(() => undefined);
    };
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, []);
  return { events, lost };
}

function EventsStep({ n }: { n: number }) {
  const { events, lost } = useEvents();
  const total = events?.total ?? 0;
  return (
    <Step n={n} title={total > 0 ? "Events are arriving" : "Send your first event"}>
      {total === 0 ? (
        <div className="waiting">
          <span className="pulse" aria-hidden="true" />
          <span className="muted">Waiting… use your app and the first event appears here.</span>
        </div>
      ) : (
        <>
          <p className="hint">
            <strong>{total.toLocaleString()}</strong> received. Triggers read them as the{" "}
            <code>events</code> table.
          </p>
          <div className="table-scroll scroll-y">
            <table>
              <thead>
                <tr>
                  <th>Received</th>
                  <th>Subscription</th>
                  <th>Operation</th>
                  <th>Data</th>
                </tr>
              </thead>
              <tbody>
                {events!.events.map((event) => (
                  <tr key={event.event_id}>
                    <td>{ago(event.received_at)}</td>
                    <td className="mono">{event.subscription}</td>
                    <td className="mono">{event.operation ?? "—"}</td>
                    <td className="mono muted" title={JSON.stringify(event.data)}>
                      {JSON.stringify(event.data).slice(0, 80)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {lost > 0 && (
        <p className="bad-text">
          Your apps reported {lost.toLocaleString()} event{lost === 1 ? "" : "s"} they couldn't
          deliver in the last 7 days.
        </p>
      )}
    </Step>
  );
}

// ------------------------------------------------------------------ steps

export function AppSteps({
  connection,
  ingestHere,
  platformId,
  frappeAppUrl,
  feedbackUrl,
}: {
  /** The app connection these steps are for, which the platform links stay under. */
  connection: string;
  ingestHere: boolean;
  platformId: string | null;
  frappeAppUrl: string | null;
  feedbackUrl: string | null;
}) {
  const keys = useKeys();
  const [asking, setAsking] = useState(false);
  const platform = PLATFORMS.find((p) => p.id === platformId) ?? null;
  const endpoint = ingestHere ? window.location.origin : null;

  return (
    <div className="connect">
      <section className="card">
        <h2>Where does your app run?</h2>
        <div className="event-grid platform-grid">
          {PLATFORMS.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`event-card platform-card${p.id === platform?.id ? " on" : ""}`}
              aria-pressed={p.id === platform?.id}
              onClick={() => navigate(href("connect", connection, p.id))}
            >
              <BrandIcon name={p.icon} size="md" label="" />
              <span>
                <strong>{p.name}</strong>
                <span className="muted">{p.blurb}</span>
              </span>
            </button>
          ))}
          <button
            type="button"
            className={`event-card platform-card${asking ? " on" : ""}`}
            aria-expanded={asking}
            onClick={() => {
              // A listener (the demo's PostHog survey) asks in place; without
              // one, the feedback link is the way to say it.
              if (!suggestHandled("platform")) setAsking(!asking);
            }}
          >
            <BrandIcon name="custom" size="md" label="" />
            <span>
              <strong>More platforms</strong>
              <span className="muted">Coming soon — tell us yours</span>
            </span>
          </button>
        </div>
        {asking && (
          <p className="notice" role="status">
            {feedbackUrl ? (
              <>
                Which platform should be next?{" "}
                <a href={feedbackUrl} target="_blank" rel="noreferrer">
                  Tell us here
                </a>
                .
              </>
            ) : (
              "Support for more platforms is on its way."
            )}
          </p>
        )}
      </section>

      {platform && !endpoint && (
        <div className="notice warn">
          This server doesn't receive events itself. Start Rowfire's ingest server with{" "}
          <code>rowfire ingest --host 0.0.0.0 --port 8100</code> and use its address wherever
          the steps below ask for this one.
        </div>
      )}

      {platform && (
        <section className="card connect-steps">
          {platform.id === "python" && (
            <PythonSteps endpoint={endpoint ?? "https://…"} keys={keys} platform={platform} />
          )}
          {platform.id === "frappe" && (
            <FrappeSteps
              endpoint={endpoint ?? "https://…"}
              keys={keys}
              platform={platform}
              appUrl={frappeAppUrl}
            />
          )}
          <EventsStep n={4} />
        </section>
      )}
    </div>
  );
}
